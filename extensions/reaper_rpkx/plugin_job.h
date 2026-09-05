// Implementation include for plugin.cpp. Ordinary PCM16 waveform jobs keep only
// fine-bucket extrema; spectral/spectrogram modes retain the bounded batch path.
struct Job{
    unsigned id=++serial;
    std::string media,cache;Stamp source_stamp;
    uint32_t rate=0,nch=0,pps=0,mtime=0,size=0;
    uint8_t format=0,mode=0;
    size_t expected=0,decoded=0;bool force=false,stream_wave=false;
    // 0=loading,1=decode,2=analysis/commit,3=ready,4=failed
    std::atomic<int>state{0};std::string error;
    std::unique_ptr<PCM_source>decoder;std::future<Result>pending;
    std::vector<int16_t>i16;std::vector<float>f32;
    std::vector<LrpkI16Extrema>wave_i16;
    std::vector<int16_t>wave_hi,wave_lo;
    std::vector<Pair>live,bucket;
    size_t live_div=1,bucket_frames=0;double decode_s=0;
    Clock::time_point started=Clock::now();std::mutex mutex;bool reported=false;
    ~Job(){if(pending.valid())pending.wait();}
    explicit Job(PCM_source*src,bool dirty,int required_mode,const std::string&cache_override={}):force(dirty){
        media=src->GetFileName()?src->GetFileName():"";
        if(media.empty()||!supported(src->GetType()))throw std::runtime_error("unsupported source");
        source_stamp=stat_file(media);
        cache=cache_override.empty()?lrpk_cache_path_for_media(media.c_str()):cache_override;
        lrpk_recover_guard(cache,force);
        const double sr=src->GetSampleRate(),len=src->GetLength();
        if(!std::isfinite(sr)||sr<1||sr>768000||!std::isfinite(len)||len<0||len*sr>double(SIZE_MAX/32))throw std::runtime_error("unsupported source geometry");
        rate=uint32_t(std::llround(sr));nch=uint32_t(src->GetNumChannels());
        if(nch<1||nch>32)throw std::runtime_error("unsupported source channel count");
        pps=uint32_t(std::max(1,cfg("peakcachegenrs",300)));mode=uint8_t(required_mode);
        const bool lossless=std::string(src->GetType())=="WAVE"||std::string(src->GetType())=="FLAC"||std::string(src->GetType())=="WAVPACK";
        format=lossless&&src->GetBitsPerSample()==16?0:(src->GetBitsPerSample()>=32&&std::string(src->GetType())=="WAVE"?2:1);
        expected=size_t(std::llround(len*rate));
        if(lrpk_stamp(media.c_str(),&mtime,&size))throw std::runtime_error(error_text());
        stream_wave=(mode==0&&format==0);
        // We never delegate a deleting clear here. A non-deleting clear is safe
        // and asks the native decoder to release any internal peak state.
        src->Peaks_Clear(false);
        {Delegating guard;decoder.reset(src->Duplicate());}
        if(!decoder)throw std::runtime_error("native decoder duplication failed");
        live_div=std::max<size_t>(1,rate/pps);bucket.resize(nch);
        if(stream_wave){
            wave_hi.assign(nch,std::numeric_limits<int16_t>::min());
            wave_lo.assign(nch,std::numeric_limits<int16_t>::max());
            const size_t fine_count=expected/live_div+size_t(expected%live_div!=0);
            wave_i16.reserve(fine_count*nch);
        }
        pending=std::async(std::launch::async,[this](){
            Result r;try{
                fs::create_directories(fs::u8path(cache).parent_path());
                // A forced rebuild cannot reuse the current standard prefix. Do
                // not take an avoidable cache handle before decoding.
                if(force)return r;
                if(!fs::exists(fs::u8path(cache)))return r;
                if(lrpk_read_standard(cache.c_str(),&r.image.b))throw std::runtime_error(error_text());
                const auto*b=r.image.b.data;const auto n=r.image.b.len;
                if(n<18)return r;
                bool spectral=false,gram=false;uint32_t fine=0;
                if(n<18+size_t(b[5])*8)throw std::runtime_error("truncated cached layer table");
                for(unsigned j=0;j<b[5];++j){const int32_t d=int32_t(u32(b+18+j*8));if(d>0&&!fine)fine=uint32_t(d);if(d==-115)spectral=true;if(d==-103)gram=true;}
                const int cached_mode=gram?2:(spectral?1:0);
                r.reuse=b[4]==nch&&u32(b+6)==rate&&u32(b+10)==mtime&&u32(b+14)==size&&fine==std::max(1u,rate/pps)&&cached_mode>=mode;
            }catch(const std::exception&e){r.error=e.what();}return r;
        });
        log("BEGIN\tid="+std::to_string(id)+"\tfile="+media+"\tcache="+cache+"\tmode="+std::to_string(mode)+"\tforce="+std::to_string(force)+"\tformat="+std::to_string(format)+"\tstream="+std::to_string(stream_wave)+"\tschedule="+std::to_string(cfg("peakcachegenmode",3)));
    }
    void fail(const std::string&s){error=s;state=4;log("ERROR\tid="+std::to_string(id)+"\t"+s);}
    void finish_bucket(){
        if(stream_wave){
            for(unsigned c=0;c<nch;++c)wave_i16.push_back({wave_hi[c],wave_lo[c]});
            std::fill(wave_hi.begin(),wave_hi.end(),std::numeric_limits<int16_t>::min());
            std::fill(wave_lo.begin(),wave_lo.end(),std::numeric_limits<int16_t>::max());
        }
        live.insert(live.end(),bucket.begin(),bucket.end());
        std::fill(bucket.begin(),bucket.end(),Pair{});
        bucket_frames=0;
    }
    int run(){
        std::lock_guard<std::mutex>g(mutex);try{
            if(state==3||state==4)return 0;
            if(state==0||state==2){
                if(pending.wait_for(std::chrono::milliseconds(0))!=std::future_status::ready){std::this_thread::sleep_for(std::chrono::milliseconds(1));return state==0?100:1;}
                Result r=pending.get();if(!r.error.empty()){fail(r.error);return 0;}
                if(state==2||r.reuse){
                    std::vector<int16_t>().swap(i16);std::vector<float>().swap(f32);std::vector<LrpkI16Extrema>().swap(wave_i16);std::vector<int16_t>().swap(wave_hi);std::vector<int16_t>().swap(wave_lo);std::vector<Pair>().swap(live);decoder.reset();state=3;
                    log("DONE\tid="+std::to_string(id)+"\treuse="+std::to_string(r.reuse)+"\ttotal_s="+std::to_string(elapsed(started))+"\tdecode_s="+std::to_string(decode_s)+"\tgenerate_s="+std::to_string(r.generation_s)+"\tcommit_s="+std::to_string(r.commit_s)+"\tstandard_written="+std::to_string(r.report.standard_bytes_written)+"\ttail_moved="+std::to_string(r.report.tail_bytes_moved)+"\tjournal_written="+std::to_string(r.report.journal_bytes_written)+"\tsyncs="+std::to_string(r.report.syncs));return 0;
                }
                const size_t bytes_per_sample=format?4:2;
                if(!stream_wave&&expected>PCM_BUDGET/(bytes_per_sample*nch))throw std::runtime_error("decoded PCM exceeds 256 MiB budget for spectral/spectrogram analysis; original cache preserved");
                state=1;
                if(!stream_wave){if(format)f32.reserve(expected*nch);else i16.reserve(expected*nch);}
            }
            const auto t=Clock::now();
            if(decoded<expected){
                const size_t count=std::min<size_t>(16384,expected-decoded);std::vector<double>samples(count*nch);
                PCM_source_transfer_t b{};b.time_s=double(decoded)/rate;b.samplerate=rate;b.nch=int(nch);b.length=int(count);b.samples=samples.data();decoder->GetSamples(&b);
                if(b.samples_out<=0||size_t(b.samples_out)>count)throw std::runtime_error("decoder returned invalid/short source; refusing partial commit");
                for(int f=0;f<b.samples_out;++f){
                    for(unsigned c=0;c<nch;++c){const double x=samples[size_t(f)*nch+c];if(!std::isfinite(x))throw std::runtime_error("nonfinite decoded sample is outside plugin validation scope");
                        if(format)f32.push_back(float(x));else{
                            const int16_t q=int16_t(std::clamp(std::llround(x*32768.),-32768LL,32767LL));
                            if(stream_wave){wave_hi[c]=std::max(wave_hi[c],q);wave_lo[c]=std::min(wave_lo[c],q);}else i16.push_back(q);
                        }
                        bucket[c].hi=std::max(bucket[c].hi,x);bucket[c].lo=std::min(bucket[c].lo,x);
                    }
                    if(++bucket_frames==live_div)finish_bucket();
                }
                decoded+=size_t(b.samples_out);
                if(size_t(b.samples_out)<count&&decoded!=expected)throw std::runtime_error("decoder length differs from source metadata");
            }
            decode_s+=elapsed(t);
            if(decoded<expected)return std::max(2,100-int(decoded*95/std::max<size_t>(expected,1)));
            if(bucket_frames)finish_bucket();
            if(!(stat_file(media)==source_stamp))throw std::runtime_error("source changed during decode");
            state=2;pending=std::async(std::launch::async,[this](){
                Result r;try{
                    auto t=Clock::now();
                    int generated=0;
                    if(stream_wave){
                        generated=lrpk_generate_wave_pcm16(wave_i16.data(),wave_i16.size(),decoded,nch,rate,pps,mtime,size,&r.image.b);
                    }else{
                        const void*p=format?static_cast<const void*>(f32.data()):static_cast<const void*>(i16.data());
                        generated=lrpk_generate(p,decoded,nch,rate,pps,mtime,size,format,mode,&r.image.b);
                    }
                    if(generated)throw std::runtime_error(error_text());
                    log("GENERATED\tid="+std::to_string(id)+"\tbytes="+std::to_string(r.image.b.len)+"\tstream="+std::to_string(stream_wave));
#ifdef LRPK_TEST_HOOKS
                    if(const char*v=std::getenv("LIBREAPEAKS_TEST_FAIL_AFTER_GENERATE")){if(!std::strcmp(v,"1"))throw std::runtime_error("TEST_GENERATOR_FAILURE_AFTER_GENERATE");}
#endif
                    r.generation_s=elapsed(t);
                    if(!(stat_file(media)==source_stamp))throw std::runtime_error("source changed during analysis");
                    t=Clock::now();
                    const std::string commit=lrpk_commit_path(cache);
                    // Preserve the original RPKX binding, including stale chunks.
                    if(lrpk_replace(commit.c_str(),r.image.b.data,r.image.b.len,1,&r.report))throw std::runtime_error(error_text());
                    lrpk_finalize_guard(cache,commit);
                    r.commit_s=elapsed(t);
                }catch(const std::exception&e){
                    const std::string cause=e.what();
                    try{lrpk_restore_guard(cache);r.error=cause;}catch(const std::exception&restore){r.error=cause+"; recovery guard restore failed: "+restore.what();}
                }return r;
            });return 1;
        }catch(const std::exception&e){
            const std::string cause=e.what();try{lrpk_restore_guard(cache);fail(cause);}catch(const std::exception&restore){fail(cause+"; recovery guard restore failed: "+restore.what());}return 0;
        }
    }
    void live_peaks(PCM_source_peaktransfer_t*b){
        std::unique_lock<std::mutex>g(mutex,std::try_to_lock);if(!g.owns_lock()||live.empty()||b->peakrate<=0||b->nchpeaks<1||b->nchpeaks>32)return;
        const size_t records=live.size()/nch;
        for(int i=0;i<b->numpeak_points;++i){const double t=b->start_time+i/b->peakrate;if(t<0||t*rate/live_div>=double(records))break;
            const size_t a=size_t(t*rate/live_div),z=size_t(std::min(double(records),std::ceil((t+1/b->peakrate)*rate/live_div)));if(a>=records)break;
            for(int c=0;c<b->nchpeaks;++c){Pair p;for(size_t j=a;j<std::max(a+1,z);++j){const auto&v=live[j*nch+unsigned(c)%nch];p.hi=std::max(p.hi,v.hi);p.lo=std::min(p.lo,v.lo);}
                b->peaks[size_t(i)*b->nchpeaks+c]=p.hi;if(b->peaks_minvals)b->peaks_minvals[size_t(i)*b->nchpeaks+c]=p.lo;
            }++b->peaks_out;
        }b->peaks_minvals_used=b->peaks_minvals?b->peaks_out:0;b->output_mode=0;
    }
};
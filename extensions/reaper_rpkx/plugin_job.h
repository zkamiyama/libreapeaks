// Implementation include for plugin.cpp. Bounded batch job; the remaining
// 256 MiB PCM limit is deliberate until the incremental DSP API is implemented.
struct Job{
    unsigned id=++serial;
    std::string media,cache;Stamp source_stamp;
    uint32_t rate=0,nch=0,pps=0,mtime=0,size=0;
    uint8_t format=0,mode=0;
    size_t expected=0,decoded=0;bool force=false;
    // 0=loading,1=decode,2=analysis/commit,3=ready,4=failed
    std::atomic<int>state{0};std::string error;
    std::unique_ptr<PCM_source>decoder;std::future<Result>pending;
    std::vector<int16_t>i16;std::vector<float>f32;
    std::vector<Pair>live,bucket;
    size_t live_div=1,bucket_frames=0;double decode_s=0;
    Clock::time_point started=Clock::now();std::mutex mutex;bool reported=false;
    ~Job(){if(pending.valid())pending.wait();}
    explicit Job(PCM_source*src,bool dirty,int required_mode):force(dirty){
        media=src->GetFileName()?src->GetFileName():"";
        if(media.empty()||!supported(src->GetType()))throw std::runtime_error("unsupported source");
        source_stamp=stat_file(media);
        char read[32768]{},write[32768]{};
        peak_name(media.c_str(),read,sizeof read,false);peak_name(media.c_str(),write,sizeof write,true);
        if(!*write)throw std::runtime_error("REAPER returned no peak path");
        if(*read&&strcmp(read,write)&&fs::exists(fs::u8path(read)))throw std::runtime_error("read/write peak paths differ; refusing implicit migration");
        cache=fs::weakly_canonical(fs::u8path(write)).u8string();
        const double sr=src->GetSampleRate(),len=src->GetLength();
        if(!std::isfinite(sr)||sr<1||sr>768000||!std::isfinite(len)||len<0||len*sr>double(SIZE_MAX/32))throw std::runtime_error("unsupported source geometry");
        rate=uint32_t(std::llround(sr));nch=uint32_t(src->GetNumChannels());
        if(nch<1||nch>32)throw std::runtime_error("unsupported source channel count");
        pps=uint32_t(std::max(1,cfg("peakcachegenrs",300)));mode=uint8_t(required_mode);
        const bool lossless=std::string(src->GetType())=="WAVE"||std::string(src->GetType())=="FLAC"||std::string(src->GetType())=="WAVPACK";
        format=lossless&&src->GetBitsPerSample()==16?0:(src->GetBitsPerSample()>=32&&std::string(src->GetType())=="WAVE"?2:1);
        expected=size_t(std::llround(len*rate));
        if(lrpk_stamp(media.c_str(),&mtime,&size))throw std::runtime_error(error_text());
        // We never delegate a deleting clear to the native source. A non-deleting
        // clear is safe and lets Windows release any peak-reader handle before
        // this job opens the RPKX-bearing cache for validation or commit.
        src->Peaks_Clear(false);
        {Delegating guard;decoder.reset(src->Duplicate());}
        if(!decoder)throw std::runtime_error("native decoder duplication failed");
        live_div=std::max<size_t>(1,rate/300);bucket.resize(nch);
        pending=std::async(std::launch::async,[this](){
            Result r;try{
                fs::create_directories(fs::u8path(cache).parent_path());
                // A forced rebuild cannot reuse the current standard prefix. Do
                // not take an avoidable read/write handle before decoding; the
                // transactional replace performs recovery and validation later.
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
        log("BEGIN\tid="+std::to_string(id)+"\tfile="+media+"\tmode="+std::to_string(mode)+"\tforce="+std::to_string(force)+"\tformat="+std::to_string(format)+"\tschedule="+std::to_string(cfg("peakcachegenmode",3)));
    }
    void fail(const std::string&s){error=s;state=4;log("ERROR\tid="+std::to_string(id)+"\t"+s);}
    int run(){
        std::lock_guard<std::mutex>g(mutex);try{
            if(state==3||state==4)return 0;
            if(state==0||state==2){
                if(pending.wait_for(std::chrono::milliseconds(0))!=std::future_status::ready){std::this_thread::sleep_for(std::chrono::milliseconds(1));return state==0?100:1;}
                Result r=pending.get();if(!r.error.empty()){fail(r.error);return 0;}
                if(state==2||r.reuse){
                    std::vector<int16_t>().swap(i16);std::vector<float>().swap(f32);std::vector<Pair>().swap(live);decoder.reset();state=3;
                    log("DONE\tid="+std::to_string(id)+"\treuse="+std::to_string(r.reuse)+"\ttotal_s="+std::to_string(elapsed(started))+"\tdecode_s="+std::to_string(decode_s)+"\tgenerate_s="+std::to_string(r.generation_s)+"\tcommit_s="+std::to_string(r.commit_s)+"\tstandard_written="+std::to_string(r.report.standard_bytes_written)+"\ttail_moved="+std::to_string(r.report.tail_bytes_moved)+"\tjournal_written="+std::to_string(r.report.journal_bytes_written)+"\tsyncs="+std::to_string(r.report.syncs));return 0;
                }
                const size_t bytes_per_sample=format?4:2;
                if(expected>PCM_BUDGET/(bytes_per_sample*nch))throw std::runtime_error("decoded PCM exceeds 256 MiB budget; original cache preserved");
                state=1;
                if(format)f32.reserve(expected*nch);else i16.reserve(expected*nch);
            }
            const auto t=Clock::now();
            if(decoded<expected){
                const size_t count=std::min<size_t>(16384,expected-decoded);std::vector<double>samples(count*nch);
                PCM_source_transfer_t b{};b.time_s=double(decoded)/rate;b.samplerate=rate;b.nch=int(nch);b.length=int(count);b.samples=samples.data();decoder->GetSamples(&b);
                if(b.samples_out<=0||size_t(b.samples_out)>count)throw std::runtime_error("decoder returned invalid/short source; refusing partial commit");
                for(int f=0;f<b.samples_out;++f){
                    for(unsigned c=0;c<nch;++c){const double x=samples[size_t(f)*nch+c];if(!std::isfinite(x))throw std::runtime_error("nonfinite decoded sample is outside plugin validation scope");
                        if(format)f32.push_back(float(x));else i16.push_back(int16_t(std::clamp(std::llround(x*32768.),-32768LL,32767LL)));
                        bucket[c].hi=std::max(bucket[c].hi,x);bucket[c].lo=std::min(bucket[c].lo,x);
                    }
                    if(++bucket_frames==live_div){live.insert(live.end(),bucket.begin(),bucket.end());std::fill(bucket.begin(),bucket.end(),Pair{});bucket_frames=0;}
                }
                decoded+=size_t(b.samples_out);
                if(size_t(b.samples_out)<count&&decoded!=expected)throw std::runtime_error("decoder length differs from source metadata");
            }
            decode_s+=elapsed(t);
            if(decoded<expected)return std::max(2,100-int(decoded*95/std::max<size_t>(expected,1)));
            if(bucket_frames){live.insert(live.end(),bucket.begin(),bucket.end());bucket_frames=0;}
            if(!(stat_file(media)==source_stamp))throw std::runtime_error("source changed during decode");
            state=2;pending=std::async(std::launch::async,[this](){
                Result r;try{
                    auto t=Clock::now();const void*p=format?static_cast<const void*>(f32.data()):static_cast<const void*>(i16.data());
                    if(lrpk_generate(p,decoded,nch,rate,pps,mtime,size,format,mode,&r.image.b))throw std::runtime_error(error_text());
                    log("GENERATED\tid="+std::to_string(id)+"\tbytes="+std::to_string(r.image.b.len));
#ifdef LRPK_TEST_HOOKS
                    if(const char*v=std::getenv("LIBREAPEAKS_TEST_FAIL_AFTER_GENERATE")){if(!std::strcmp(v,"1"))throw std::runtime_error("TEST_GENERATOR_FAILURE_AFTER_GENERATE");}
#endif
                    r.generation_s=elapsed(t);
                    if(!(stat_file(media)==source_stamp))throw std::runtime_error("source changed during analysis");
                    t=Clock::now();
                    // Preserve the original RPKX binding, including stale chunks.
                    if(lrpk_replace(cache.c_str(),r.image.b.data,r.image.b.len,1,&r.report))throw std::runtime_error(error_text());
                    r.commit_s=elapsed(t);
                }catch(const std::exception&e){r.error=e.what();}return r;
            });return 1;
        }catch(const std::exception&e){fail(e.what());return 0;}
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
// Implementation include for plugin.cpp. Ordinary PCM16 waveform jobs keep only
// fine-bucket extrema; spectral/spectrogram modes retain the bounded batch path.
#include "raw_pcm16_wave.h"
#include <condition_variable>
#include <deque>
#include <functional>
#include <utility>

#ifndef LRPK_TEST_HOOKS
// Starting a fresh std::async worker was measurably slower than REAPER's native
// 10-second waveform build on Windows even though raw decode+generation already
// beat native. Start one durability worker with the DLL instead; peak builders
// only enqueue a tiny closure after the complete standard image is available.
class CommitWorker{
    std::mutex mutex;
    std::condition_variable cv;
    std::deque<std::function<void()>> queue;
    bool stopping=false;
    std::thread worker;
    void loop(){
        for(;;){
            std::function<void()> fn;
            {
                std::unique_lock<std::mutex>g(mutex);
                cv.wait(g,[this](){return stopping||!queue.empty();});
                if(stopping&&queue.empty())return;
                fn=std::move(queue.front());queue.pop_front();
            }
            fn();
        }
    }
public:
    CommitWorker():worker([this](){loop();}){}
    ~CommitWorker(){
        {std::lock_guard<std::mutex>g(mutex);stopping=true;}
        cv.notify_all();if(worker.joinable())worker.join();
    }
    template<class F>auto submit(F f)->std::future<decltype(f())>{
        using R=decltype(f());
        auto task=std::make_shared<std::packaged_task<R()>>(std::move(f));
        auto future=task->get_future();
        {
            std::lock_guard<std::mutex>g(mutex);
            if(stopping)throw std::runtime_error("waveform commit worker is stopping");
            queue.emplace_back([task](){(*task)();});
        }
        cv.notify_one();return future;
    }
};
static CommitWorker commit_worker;
#endif

struct Job{
    unsigned id=++serial;
    std::string media,cache;Stamp source_stamp;
    uint32_t rate=0,nch=0,pps=0,mtime=0,size=0;
    uint8_t format=0,mode=0;
    size_t expected=0,decoded=0;bool force=false,stream_wave=false,raw_pcm16=false,async_commit=false;
    // 0=loading,1=decode,2=peak-ready/durable-commit-pending,3=durable-ready,4=failed
    std::atomic<int>state{0};std::string error;
    std::unique_ptr<PCM_source>decoder;std::future<Result>pending;std::future<void>durable;RawPcm16Wave raw;
    std::vector<int16_t>i16;std::vector<float>f32;std::vector<double>decode_block;
    std::vector<LrpkI16Extrema>wave_i16;
    std::vector<int16_t>wave_hi,wave_lo;
    std::vector<Pair>live,bucket;
    size_t live_div=1,bucket_frames=0;double decode_s=0;
    Clock::time_point started=Clock::now();std::mutex mutex;bool reported=false;
    ~Job(){if(pending.valid())pending.wait();if(durable.valid())durable.wait();}
    Result inspect_cache(){
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
    }
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
        const std::string type=src->GetType();
        const bool lossless=type=="WAVE"||type=="FLAC"||type=="WAVPACK";
        format=lossless&&src->GetBitsPerSample()==16?0:(src->GetBitsPerSample()>=32&&type=="WAVE"?2:1);
        expected=size_t(std::llround(len*rate));
        if(lrpk_stamp(media.c_str(),&mtime,&size))throw std::runtime_error(error_text());
        stream_wave=(mode==0&&format==0);
        // We never delegate a deleting clear here. A non-deleting clear is safe
        // and asks the native decoder to release any internal peak state.
        src->Peaks_Clear(false);
        // Canonical PCM16 RIFF/WAVE needs no conversion through double samples,
        // including spectral/spectrogram jobs. The strict parser refuses every
        // ambiguous geometry and then we fall back to REAPER's decoder.
        raw_pcm16=format==0&&type=="WAVE"&&raw.open(media,rate,nch,expected);
#ifndef LRPK_TEST_HOOKS
        // Only waveform mode can expose complete live extrema before durability.
        // Spectral/spectrogram output still waits for its synchronous commit.
        async_commit=raw_pcm16&&stream_wave;
#endif
        if(!raw_pcm16){
            {Delegating guard;decoder.reset(src->Duplicate());}
            if(!decoder)throw std::runtime_error("native decoder duplication failed");
        }
        live_div=std::max<size_t>(1,rate/pps);bucket.resize(nch);
        if(stream_wave){
            wave_hi.assign(nch,std::numeric_limits<int16_t>::min());
            wave_lo.assign(nch,std::numeric_limits<int16_t>::max());
            const size_t fine_count=expected/live_div+size_t(expected%live_div!=0);
            wave_i16.reserve(fine_count*nch);
        }
        // Raw PCM16 jobs inspect only the small standard prefix synchronously on
        // their first Run call, avoiding a per-job OS thread launch.
        if(!raw_pcm16)pending=std::async(std::launch::async,[this](){return inspect_cache();});
        log("BEGIN\tid="+std::to_string(id)+"\tfile="+media+"\tcache="+cache+"\tmode="+std::to_string(mode)+"\tforce="+std::to_string(force)+"\tformat="+std::to_string(format)+"\tstream="+std::to_string(stream_wave)+"\traw_pcm16="+std::to_string(raw_pcm16)+"\tasync_commit="+std::to_string(async_commit)+"\tschedule="+std::to_string(cfg("peakcachegenmode",3)));
    }
    void fail(const std::string&s){error=s;state=4;log("ERROR\tid="+std::to_string(id)+"\t"+s);}
    void release_buffers(){
        std::vector<int16_t>().swap(i16);std::vector<float>().swap(f32);std::vector<double>().swap(decode_block);
        std::vector<LrpkI16Extrema>().swap(wave_i16);std::vector<int16_t>().swap(wave_hi);std::vector<int16_t>().swap(wave_lo);
        std::vector<Pair>().swap(live);decoder.reset();raw.close();
    }
    void done(const Result&r,bool reuse){
        release_buffers();state=3;
        log("DONE\tid="+std::to_string(id)+"\treuse="+std::to_string(reuse)+"\traw_pcm16="+std::to_string(raw_pcm16)+"\tasync_commit="+std::to_string(async_commit)+"\ttotal_s="+std::to_string(elapsed(started))+"\tdecode_s="+std::to_string(decode_s)+"\tgenerate_s="+std::to_string(r.generation_s)+"\tcommit_s="+std::to_string(r.commit_s)+"\tstandard_written="+std::to_string(r.report.standard_bytes_written)+"\ttail_moved="+std::to_string(r.report.tail_bytes_moved)+"\tjournal_written="+std::to_string(r.report.journal_bytes_written)+"\tsyncs="+std::to_string(r.report.syncs));
    }
    void finish_bucket(){
        if(stream_wave){
            for(unsigned c=0;c<nch;++c){
                const int16_t hi=wave_hi[c],lo=wave_lo[c];wave_i16.push_back({hi,lo});
                if(raw_pcm16)live.push_back({double(hi)/32768.,double(lo)/32768.});
            }
            if(!raw_pcm16)live.insert(live.end(),bucket.begin(),bucket.end());
            std::fill(wave_hi.begin(),wave_hi.end(),std::numeric_limits<int16_t>::min());
            std::fill(wave_lo.begin(),wave_lo.end(),std::numeric_limits<int16_t>::max());
        }else live.insert(live.end(),bucket.begin(),bucket.end());
        if(!(stream_wave&&raw_pcm16))std::fill(bucket.begin(),bucket.end(),Pair{});
        bucket_frames=0;
    }
    int run(){
        std::lock_guard<std::mutex>g(mutex);try{
            if(state==3||state==4)return 0;
            if(state==2&&async_commit)return 0;
            if(state==0||state==2){
                Result r;
                if(state==0&&raw_pcm16)r=inspect_cache();
                else{
                    if(!pending.valid())throw std::runtime_error("peak worker state lost");
                    if(pending.wait_for(std::chrono::milliseconds(0))!=std::future_status::ready){std::this_thread::sleep_for(std::chrono::milliseconds(1));return state==0?100:1;}
                    r=pending.get();
                }
                if(!r.error.empty()){fail(r.error);return 0;}
                if(state==2||r.reuse){done(r,r.reuse);return 0;}
                const size_t bytes_per_sample=format?4:2;
                if(!stream_wave&&expected>PCM_BUDGET/(bytes_per_sample*nch))throw std::runtime_error("decoded PCM exceeds 256 MiB budget for spectral/spectrogram analysis; original cache preserved");
                state=1;
                if(!stream_wave){if(format)f32.reserve(expected*nch);else i16.reserve(expected*nch);}
            }
            const auto t=Clock::now();
            if(decoded<expected){
                // Direct PCM16 reading can consume multiple large chunks inside
                // the same scheduler slice. Decoder fallback keeps smaller blocks.
                do{
                    if(raw_pcm16){
                        const size_t count=std::min<size_t>(65536,expected-decoded),got=raw.read_frames(count,nch);
                        if(got!=count)throw std::runtime_error("raw PCM16 WAV changed/truncated during peak decode");
                        const uint8_t*s=raw.block.data();
                        for(size_t f=0;f<got;++f){
                            for(unsigned c=0;c<nch;++c){
                                const size_t off=(f*nch+c)*2;const uint16_t u=uint16_t(s[off])|(uint16_t(s[off+1])<<8);const int32_t v=(u&0x8000)?int32_t(u)-65536:int32_t(u);const int16_t q=int16_t(v);
                                if(stream_wave){wave_hi[c]=std::max(wave_hi[c],q);wave_lo[c]=std::min(wave_lo[c],q);}
                                else{i16.push_back(q);const double x=double(q)/32768.;bucket[c].hi=std::max(bucket[c].hi,x);bucket[c].lo=std::min(bucket[c].lo,x);}
                            }
                            if(++bucket_frames==live_div)finish_bucket();
                        }
                        decoded+=got;
                    }else{
                        const size_t count=std::min<size_t>(16384,expected-decoded);decode_block.resize(count*nch);
                        PCM_source_transfer_t b{};b.time_s=double(decoded)/rate;b.samplerate=rate;b.nch=int(nch);b.length=int(count);b.samples=decode_block.data();decoder->GetSamples(&b);
                        if(b.samples_out<=0||size_t(b.samples_out)>count)throw std::runtime_error("decoder returned invalid/short source; refusing partial commit");
                        for(int f=0;f<b.samples_out;++f){
                            for(unsigned c=0;c<nch;++c){const double x=decode_block[size_t(f)*nch+c];if(!std::isfinite(x))throw std::runtime_error("nonfinite decoded sample is outside plugin validation scope");
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
                }while((stream_wave||raw_pcm16)&&decoded<expected&&elapsed(t)<0.008);
            }
            decode_s+=elapsed(t);
            if(decoded<expected)return std::max(2,100-int(decoded*95/std::max<size_t>(expected,1)));
            if(bucket_frames)finish_bucket();
            raw.close();
            if(!(stat_file(media)==source_stamp))throw std::runtime_error("source changed during decode");
            if(async_commit){
                Result r;auto gt=Clock::now();
                if(lrpk_generate_wave_pcm16(wave_i16.data(),wave_i16.size(),decoded,nch,rate,pps,mtime,size,&r.image.b))throw std::runtime_error(error_text());
                r.generation_s=elapsed(gt);
                log("GENERATED\tid="+std::to_string(id)+"\tbytes="+std::to_string(r.image.b.len)+"\tstream=1\traw_pcm16=1\tasync_commit=1");
                if(!(stat_file(media)==source_stamp))throw std::runtime_error("source changed during analysis");
                state=2;
#ifndef LRPK_TEST_HOOKS
                durable=commit_worker.submit([this,r=std::move(r)]()mutable{
                    try{
                        auto t=Clock::now();const std::string commit=lrpk_commit_path(cache);
                        if(lrpk_replace(commit.c_str(),r.image.b.data,r.image.b.len,1,&r.report))throw std::runtime_error(error_text());
                        lrpk_finalize_guard(cache,commit);r.commit_s=elapsed(t);
                    }catch(const std::exception&e){
                        const std::string cause=e.what();
                        try{lrpk_restore_guard(cache);r.error=cause;}catch(const std::exception&restore){r.error=cause+"; recovery guard restore failed: "+restore.what();}
                    }
                    std::lock_guard<std::mutex>g(mutex);
                    if(!r.error.empty())fail(r.error);else done(r,false);
                });
#endif
                return 0;
            }
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
                    log("GENERATED\tid="+std::to_string(id)+"\tbytes="+std::to_string(r.image.b.len)+"\tstream="+std::to_string(stream_wave)+"\traw_pcm16="+std::to_string(raw_pcm16)+"\tasync_commit=0");
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
            raw.close();const std::string cause=e.what();try{lrpk_restore_guard(cache);fail(cause);}catch(const std::exception&restore){fail(cause+"; recovery guard restore failed: "+restore.what());}return 0;
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
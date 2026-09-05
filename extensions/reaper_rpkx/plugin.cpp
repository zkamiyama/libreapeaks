// Experimental REAPER extension. Native PCM decoding and read-only peak getters
// are retained. No native peak writer entry point is imported or called.
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include "reaper_plugin.h"
#include "bridge.h"
#ifdef min
#undef min
#endif
#ifdef max
#undef max
#endif

namespace fs=std::filesystem;
using Clock=std::chrono::steady_clock;
static PCM_source* (*create_file)(const char*);
static PCM_source* (*create_type)(const char*);
static void (*peak_name)(const char*,char*,int,bool);
static REAPER_PeakGet_Interface* (*peak_get)(const char*,int,int);
static void (*hires)(PCM_source*,PCM_source_peaktransfer_t*);
static void* (*config_var)(const char*,int*);
static void (*console)(const char*);
static int (*regfn)(const char*,void*);
static const char* (*app_version)();
static thread_local unsigned delegate_depth=0;
static std::mutex log_mu;
static std::string log_path;
static std::atomic<unsigned> serial{0};
static constexpr size_t PCM_BUDGET=256*1024*1024;
static double elapsed(Clock::time_point a){return std::chrono::duration<double>(Clock::now()-a).count();}
static std::string error_text(){char b[2048]{};lrpk_last_error(b,sizeof b);return b;}
static void log(const std::string&s){std::lock_guard<std::mutex>g(log_mu);if(!log_path.empty()){std::ofstream f(fs::u8path(log_path),std::ios::app);f<<s<<'\n';}}
static uint32_t u32(const uint8_t*p){return uint32_t(p[0])|uint32_t(p[1])<<8|uint32_t(p[2])<<16|uint32_t(p[3])<<24;}
static int cfg(const char*n,int fallback){int size=0;auto*p=config_var?static_cast<int*>(config_var(n,&size)):nullptr;return p&&size==sizeof(int)?*p:fallback;}
static int requested_mode(){const int s=cfg("showpeaks",1);return (s&256)?2:((s&(32|8192|16384|32768|262144))?1:0);}
struct Delegating{Delegating(){++delegate_depth;}~Delegating(){--delegate_depth;}};
struct Buffer{
    LrpkBuffer b{};
    Buffer()=default;~Buffer(){lrpk_free(&b);}
    Buffer(const Buffer&)=delete;Buffer&operator=(const Buffer&)=delete;
    Buffer(Buffer&&o)noexcept:b(o.b){o.b={};}
    Buffer&operator=(Buffer&&o)noexcept{if(this!=&o){lrpk_free(&b);b=o.b;o.b={};}return*this;}
};
struct Stamp{
    uintmax_t size=0;fs::file_time_type time{};
    bool operator==(const Stamp&o)const{return size==o.size&&time==o.time;}
};
static Stamp stat_file(const std::string&p){auto f=fs::u8path(p);return {fs::file_size(f),fs::last_write_time(f)};}
static bool supported(const char*t){return t&&(!strcmp(t,"WAVE")||!strcmp(t,"FLAC")||!strcmp(t,"MP3")||!strcmp(t,"VORBIS")||!strcmp(t,"WAVPACK"));}
struct Pair{double hi=-1e300,lo=1e300;};
struct Result{Buffer image;std::string error;bool reuse=false;LrpkReport report{};double generation_s=0,commit_s=0;};
struct Job{
    unsigned id=++serial;
    std::string media,cache;
    Stamp source_stamp;
    uint32_t rate=0,nch=0,pps=0,mtime=0,size=0;
    uint8_t format=0,mode=0;
    size_t expected=0,decoded=0;
    bool force=false;
    // 0=loading,1=decode,2=analysis/commit,3=ready,4=failed
    std::atomic<int> state{0};
    std::string error;
    std::unique_ptr<PCM_source> decoder;
    std::future<Result> pending;
    std::vector<int16_t> i16;
    std::vector<float> f32;
    std::vector<Pair> live;
    std::vector<Pair> bucket;
    size_t live_div=1,bucket_frames=0;
    double decode_s=0;
    Clock::time_point started=Clock::now();
    std::mutex mutex;
    bool reported=false;
    ~Job(){if(pending.valid())pending.wait();}
    explicit Job(PCM_source*src,bool dirty):force(dirty){
        media=src->GetFileName()?src->GetFileName():"";
        if(media.empty()||!supported(src->GetType()))throw std::runtime_error("unsupported source");
        source_stamp=stat_file(media);
        char read[32768]{},write[32768]{};
        peak_name(media.c_str(),read,sizeof read,false);peak_name(media.c_str(),write,sizeof write,true);
        if(!*write)throw std::runtime_error("REAPER returned no peak path");
        // Do not silently migrate an existing cache between different paths.
        if(*read&&strcmp(read,write)&&fs::exists(fs::u8path(read)))throw std::runtime_error("read/write peak paths differ; refusing implicit migration");
        cache=fs::weakly_canonical(fs::u8path(write)).u8string();
        const double sr=src->GetSampleRate(),len=src->GetLength();
        if(!std::isfinite(sr)||sr<1||sr>768000||!std::isfinite(len)||len<0||len*sr>double(SIZE_MAX/32))throw std::runtime_error("unsupported source geometry");
        rate=uint32_t(std::llround(sr));nch=uint32_t(src->GetNumChannels());
        pps=uint32_t(std::max(1,cfg("peakcachegenrs",300)));mode=uint8_t(requested_mode());
        if(cfg("peakcachegenmode",3)!=3)throw std::runtime_error("only peakcachegenmode=3 is validated; native writer remains disabled");
        const bool lossless=std::string(src->GetType())=="WAVE"||std::string(src->GetType())=="FLAC"||std::string(src->GetType())=="WAVPACK";
        format=lossless&&src->GetBitsPerSample()==16?0:(src->GetBitsPerSample()>=32&&std::string(src->GetType())=="WAVE"?2:1);
        expected=size_t(std::llround(len*rate));
        const size_t bytes_per_sample=format?4:2;
        if(nch<1||nch>32||expected>PCM_BUDGET/(bytes_per_sample*nch))throw std::runtime_error("decoded PCM exceeds 256 MiB budget; original cache preserved");
        if(lrpk_stamp(media.c_str(),&mtime,&size))throw std::runtime_error(error_text());
        {Delegating guard;decoder.reset(src->Duplicate());}
        if(!decoder)throw std::runtime_error("native decoder duplication failed");
        live_div=std::max<size_t>(1,rate/300);bucket.resize(nch);
        pending=std::async(std::launch::async,[this](){
            Result r;
            try{
                fs::create_directories(fs::u8path(cache).parent_path());
                if(!fs::exists(fs::u8path(cache)))return r;
                if(lrpk_read_standard(cache.c_str(),&r.image.b))throw std::runtime_error(error_text());
                const auto*b=r.image.b.data;const auto n=r.image.b.len;
                if(n<18)return r;
                bool spectral=false,gram=false;uint32_t fine=0;
                if(n<18+size_t(b[5])*8)throw std::runtime_error("truncated cached layer table");
                for(unsigned j=0;j<b[5];++j){const int32_t d=int32_t(u32(b+18+j*8));if(d>0&&!fine)fine=uint32_t(d);if(d==-115)spectral=true;if(d==-103)gram=true;}
                const int cached_mode=gram?2:(spectral?1:0);
                r.reuse=!force&&b[4]==nch&&u32(b+6)==rate&&u32(b+10)==mtime&&u32(b+14)==size&&fine==std::max(1u,rate/pps)&&cached_mode>=mode;
            }catch(const std::exception&e){r.error=e.what();}return r;
        });
        log("BEGIN\tid="+std::to_string(id)+"\tfile="+media+"\tmode="+std::to_string(mode)+"\tforce="+std::to_string(force)+"\tformat="+std::to_string(format));
    }
    void fail(const std::string&s){error=s;state=4;log("ERROR\tid="+std::to_string(id)+"\t"+s);}
    int run(){
        std::lock_guard<std::mutex>g(mutex);
        try{
            if(state==3||state==4)return 0;
            if(state==0||state==2){
                if(pending.wait_for(std::chrono::milliseconds(0))!=std::future_status::ready){std::this_thread::sleep_for(std::chrono::milliseconds(1));return state==0?100:1;}
                Result r=pending.get();
                if(!r.error.empty()){fail(r.error);return 0;}
                if(state==2||r.reuse){
                    std::vector<int16_t>().swap(i16);std::vector<float>().swap(f32);
                    std::vector<Pair>().swap(live);decoder.reset();
                    state=3;
                    log("DONE\tid="+std::to_string(id)+"\treuse="+std::to_string(r.reuse)+"\ttotal_s="+std::to_string(elapsed(started))+"\tdecode_s="+std::to_string(decode_s)+"\tgenerate_s="+std::to_string(r.generation_s)+"\tcommit_s="+std::to_string(r.commit_s)+"\tstandard_written="+std::to_string(r.report.standard_bytes_written)+"\ttail_moved="+std::to_string(r.report.tail_bytes_moved)+"\tjournal_written="+std::to_string(r.report.journal_bytes_written)+"\tsyncs="+std::to_string(r.report.syncs));
                    return 0;
                }
                state=1;
                if(format)i16.clear();else f32.clear();
                if(format)f32.reserve(expected*nch);else i16.reserve(expected*nch);
            }
            const auto t=Clock::now();
            if(decoded<expected){
                const size_t count=std::min<size_t>(16384,expected-decoded);
                std::vector<double> samples(count*nch);
                PCM_source_transfer_t b{};b.time_s=double(decoded)/rate;b.samplerate=rate;b.nch=int(nch);b.length=int(count);b.samples=samples.data();
                decoder->GetSamples(&b);
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
            state=2;
            pending=std::async(std::launch::async,[this](){
                Result r;try{
                    auto t=Clock::now();const void*p=format?static_cast<const void*>(f32.data()):static_cast<const void*>(i16.data());
                    if(lrpk_generate(p,decoded,nch,rate,pps,mtime,size,format,mode,&r.image.b))throw std::runtime_error(error_text());
                    r.generation_s=elapsed(t);
                    if(!(stat_file(media)==source_stamp))throw std::runtime_error("source changed during analysis");
                    t=Clock::now();
                    // Keep the original RPKX binding. Changed-source chunks are
                    // preserved as stale, never silently rebound or deleted.
                    if(lrpk_replace(cache.c_str(),r.image.b.data,r.image.b.len,1,&r.report))throw std::runtime_error(error_text());
                    r.commit_s=elapsed(t);
                }catch(const std::exception&e){r.error=e.what();}return r;
            });
            return 1;
        }catch(const std::exception&e){fail(e.what());return 0;}
    }
    void live_peaks(PCM_source_peaktransfer_t*b){
        std::unique_lock<std::mutex>g(mutex,std::try_to_lock);if(!g.owns_lock()||live.empty()||b->peakrate<=0||b->nchpeaks<1||b->nchpeaks>32)return;
        size_t records=live.size()/nch;
        for(int i=0;i<b->numpeak_points;++i){const double t=b->start_time+i/b->peakrate;if(t<0)break;
            if(t*rate/live_div>=double(records))break;
            const size_t a=size_t(t*rate/live_div),z=size_t(std::min(double(records),std::ceil((t+1/b->peakrate)*rate/live_div)));
            if(a>=records)break;
            for(int c=0;c<b->nchpeaks;++c){Pair p;for(size_t j=a;j<std::max(a+1,z);++j){const auto&v=live[j*nch+unsigned(c)%nch];p.hi=std::max(p.hi,v.hi);p.lo=std::min(p.lo,v.lo);}
                b->peaks[size_t(i)*b->nchpeaks+c]=p.hi;if(b->peaks_minvals)b->peaks_minvals[size_t(i)*b->nchpeaks+c]=p.lo;
            }++b->peaks_out;
        }b->peaks_minvals_used=b->peaks_minvals?b->peaks_out:0;b->output_mode=0;
    }
};
static std::mutex jobs_mu;
static std::map<std::string,std::weak_ptr<Job>> jobs;
class Source;
static std::set<Source*> sources;
static std::mutex sources_mu;
class Source final:public PCM_source{
    std::unique_ptr<PCM_source> inner;
    std::unique_ptr<REAPER_PeakGet_Interface> getter;
    Stamp getter_stamp;
    std::string cache,error;
    std::shared_ptr<Job> job;
    bool dirty=false;
public:
    explicit Source(PCM_source*p):inner(p){std::lock_guard<std::mutex>g(sources_mu);sources.insert(this);}
    ~Source()override{std::lock_guard<std::mutex>g(sources_mu);sources.erase(this);}
    PCM_source*Duplicate()override{try{Delegating g;auto*p=inner->Duplicate();return p?new Source(p):nullptr;}catch(...){return nullptr;}}
    bool IsAvailable()override{return inner->IsAvailable();}
    void SetAvailable(bool v)override{getter.reset();inner->SetAvailable(v);}
    const char*GetType()override{return inner->GetType();}
    const char*GetFileName()override{return inner->GetFileName();}
    bool SetFileName(const char*s)override{getter.reset();job.reset();cache.clear();return inner->SetFileName(s);}
    PCM_source*GetSource()override{return inner->GetSource();}
    void SetSource(PCM_source*p)override{inner->SetSource(p);}
    int GetNumChannels()override{return inner->GetNumChannels();}
    double GetSampleRate()override{return inner->GetSampleRate();}
    double GetLength()override{return inner->GetLength();}
    double GetLengthBeats()override{return inner->GetLengthBeats();}
    int GetBitsPerSample()override{return inner->GetBitsPerSample();}
    double GetPreferredPosition()override{return inner->GetPreferredPosition();}
    int PropertiesWindow(HWND h)override{return inner->PropertiesWindow(h);}
    void GetSamples(PCM_source_transfer_t*b)override{inner->GetSamples(b);}
    void SaveState(ProjectStateContext*c)override{inner->SaveState(c);}
    int LoadState(const char*s,ProjectStateContext*c)override{getter.reset();cache.clear();job.reset();return inner->LoadState(s,c);}
    int Extended(int c,void*a,void*b,void*d)override{return inner->Extended(c,a,b,d);}
    void Peaks_Clear(bool remove)override{
        getter.reset();dirty=dirty||remove;job.reset();error.clear();
        log("CLEAR\tdelete_requested="+std::to_string(remove)+"\tfile="+(GetFileName()?GetFileName():""));
        // Deliberately do NOT call inner->Peaks_Clear or a native builder.
    }
    int PeaksBuild_Begin()override{
        try{
            if(job&&job->state<3)return 1;
            getter.reset();error.clear();
            const std::string media=GetFileName()?GetFileName():"";
            const auto st=stat_file(media);
            const std::string key=media+"|"+std::to_string(st.size)+"|"+std::to_string(st.time.time_since_epoch().count())+"|"+std::to_string(requested_mode())+"|"+std::to_string(cfg("peakcachegenrs",300));
            std::lock_guard<std::mutex>g(jobs_mu);
            auto existing=jobs[key].lock();
            if(existing&&existing->state<3&&(!dirty||existing->force))job=existing;
            else{job=std::make_shared<Job>(inner.get(),dirty);jobs[key]=job;}
            dirty=false;cache=job->cache;return 1;
        }catch(const std::exception&e){error=e.what();log("ERROR\tbegin\t"+error);if(console)console(("libreapeaks: "+error+"\n").c_str());return 0;}
    }
    int PeaksBuild_Run()override{return job?job->run():0;}
    void PeaksBuild_Finish()override{
        if(job&&job->state==4){error=job->error;if(!job->reported&&console){console(("libreapeaks: "+error+"\n").c_str());job->reported=true;}}
        // Finish may also mean cancellation. Never commit an incomplete decode.
        if(job&&job->state<2){job.reset();dirty=true;}
        getter.reset();
    }
    void GetPeakInfo(PCM_source_peaktransfer_t*b)override{
        b->peaks_out=0;b->peaks_minvals_used=0;b->extra_requested_data_out=0;b->extra_requested_data_out2=0;
        if(!b->peaks||b->numpeak_points<1||b->numpeak_points>1000000||!std::isfinite(b->start_time)||!std::isfinite(b->peakrate))return;
        try{
            if(job&&job->state<3){job->live_peaks(b);return;}
            if(cache.empty()){const char*fn=GetFileName();if(!fn||!*fn)return;char p[32768]{};peak_name(fn,p,sizeof p,false);cache=p;}
            void*guard=lrpk_try_read_guard(cache.c_str());if(!guard)return;
            struct Guard{void*p;~Guard(){lrpk_release_read_guard(p);}}g{guard};
            const auto stamp=stat_file(cache);
            if(!getter||!(stamp==getter_stamp)){getter.reset();getter.reset(peak_get(GetFileName(),int(GetSampleRate()),GetNumChannels()));getter_stamp=stamp;}
            if(getter){
                const double maxres=REAPER_PEAKRES_MAX_FOR_BLOCK(b,getter->GetMaxPeakRes());
                if(hires&&b->peakrate>=maxres){b->__peakgetter=getter.get();hires(inner.get(),b);b->__peakgetter=nullptr;if(b->peaks_out)return;}
                getter->GetPeakInfo(b);
            }
        }catch(const std::exception&e){error=e.what();}
    }
    int status()const{return !error.empty()||(job&&job->state==4)?-1:(job?(job->state==3?2:1):0);}
};
static PCM_source* from_file(const char*p,int priority){
    if(!p||priority||delegate_depth)return nullptr;
    try{Delegating g;auto*src=create_file(p);if(!src)return nullptr;if(!supported(src->GetType()))return src;return new Source(src);}catch(...){return nullptr;}
}
static PCM_source* from_type(const char*p,int priority){
    if(!supported(p)||priority||delegate_depth)return nullptr;
    try{Delegating g;auto*src=create_type(p);return src?new Source(src):nullptr;}catch(...){return nullptr;}
}
static const char*extensions(int,const char**desc){if(desc)*desc=nullptr;return nullptr;}
static pcmsrc_register_t provider={from_type,from_file,extensions};
static Source*checked(PCM_source*p){std::lock_guard<std::mutex>g(sources_mu);auto*s=static_cast<Source*>(p);return sources.count(s)?s:nullptr;}
static int force_build(PCM_source*p){if(auto*s=checked(p)){s->Peaks_Clear(true);return 1;}return 0;}
static int status(PCM_source*p){if(auto*s=checked(p))return s->status();return -2;}
static void*force_va(void**a,int n){return reinterpret_cast<void*>(static_cast<intptr_t>(n==1?force_build(static_cast<PCM_source*>(a[0])):0));}
static void*status_va(void**a,int n){return reinterpret_cast<void*>(static_cast<intptr_t>(n==1?status(static_cast<PCM_source*>(a[0])):-2));}
extern "C" REAPER_PLUGIN_DLL_EXPORT int REAPER_PLUGIN_ENTRYPOINT(REAPER_PLUGIN_HINSTANCE,reaper_plugin_info_t*r){
    if(!r){if(regfn)regfn("-pcmsrc",&provider);return 0;}
    if(r->caller_version!=REAPER_PLUGIN_VERSION||!r->GetFunc||!r->Register)return 0;
    regfn=r->Register;
#define LOAD(name,var) var=reinterpret_cast<decltype(var)>(r->GetFunc(name));if(!var)return 0
    LOAD("PCM_Source_CreateFromFile",create_file);LOAD("PCM_Source_CreateFromType",create_type);
    LOAD("GetPeakFileNameEx",peak_name);LOAD("PeakGet_Create",peak_get);LOAD("get_config_var",config_var);
    LOAD("ShowConsoleMsg",console);LOAD("GetAppVersion",app_version);
#undef LOAD
    hires=reinterpret_cast<decltype(hires)>(r->GetFunc("HiresPeaksFromSource"));
    if(const char*p=std::getenv("LIBREAPEAKS_PLUGIN_LOG"))log_path=p;
    log(std::string("LOAD\tversion=")+app_version());
    if(std::strncmp(app_version(),"7.79",4)){console("libreapeaks experimental plugin is pinned to REAPER 7.79; it was not loaded.\n");return 0;}
    if(!r->Register("<pcmsrc",&provider))return 0;
    r->Register("ext_name",const_cast<char*>("libreapeaks RPKX protection (experimental)"));
    r->Register("ext_vendor",const_cast<char*>("libreapeaks"));
    r->Register("API_RPKX_ForceBuild",reinterpret_cast<void*>(force_build));
    r->Register("APIdef_RPKX_ForceBuild",const_cast<char*>("int\0PCM_source*\0source\0Mark a wrapped source for a preserving rebuild."));
    r->Register("APIvararg_RPKX_ForceBuild",reinterpret_cast<void*>(force_va));
    r->Register("API_RPKX_Status",reinterpret_cast<void*>(status));
    r->Register("APIdef_RPKX_Status",const_cast<char*>("int\0PCM_source*\0source\0-2 not wrapped; -1 failed; 0 idle; 1 busy; 2 ready."));
    r->Register("APIvararg_RPKX_Status",reinterpret_cast<void*>(status_va));
    return 1;
}

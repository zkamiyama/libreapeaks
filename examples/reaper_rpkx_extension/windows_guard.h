#pragma once
// Windows REAPER can keep an RPKX-bearing peak file open without FILE_SHARE_WRITE.
// For a host-requested destructive clear we first prove the current cache is
// structurally preservable, then create a hard-link guard, and only then allow
// the native clear/delete operation. Native peak generation remains intercepted
// by Source::PeaksBuild_*; the plugin updates the guarded inode with the normal
// WAL transaction and moves the guard name back into place.
static fs::path lrpk_guard_path(const std::string& cache){return fs::u8path(cache+".lrpk.guard");}
static std::string lrpk_cache_path_for_media(const char* media){
    if(!media||!*media)throw std::runtime_error("source has no media path");
    char read[32768]{},write[32768]{};
    peak_name(media,read,sizeof read,false);peak_name(media,write,sizeof write,true);
    if(!*write)throw std::runtime_error("REAPER returned no peak path");
    if(*read&&strcmp(read,write)&&fs::exists(fs::u8path(read)))throw std::runtime_error("read/write peak paths differ; refusing implicit migration");
    return fs::weakly_canonical(fs::u8path(write)).u8string();
}
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

struct LrpkWindowsCommitLease{
    HANDLE mutex=nullptr;
    HANDLE pin=INVALID_HANDLE_VALUE;
};
static thread_local LrpkWindowsCommitLease lrpk_windows_commit_lease;
static uint64_t lrpk_windows_cache_hash(const std::string&cache){
    uint64_t h=1469598103934665603ull;
    for(unsigned char c:cache){h^=uint64_t(c);h*=1099511628211ull;}
    return h;
}
static void lrpk_release_windows_commit_lease(){
    auto&lease=lrpk_windows_commit_lease;
    if(lease.pin!=INVALID_HANDLE_VALUE){CloseHandle(lease.pin);lease.pin=INVALID_HANDLE_VALUE;}
    if(lease.mutex){ReleaseMutex(lease.mutex);CloseHandle(lease.mutex);lease.mutex=nullptr;}
}
static void lrpk_acquire_windows_commit_lease(const std::string&cache){
    auto&lease=lrpk_windows_commit_lease;
    if(lease.mutex)throw std::runtime_error("Windows cache commit lease was already held");
    char name[96]{};
    std::snprintf(name,sizeof name,"Local\\libreapeaks-rpkx-commit-%016llx",static_cast<unsigned long long>(lrpk_windows_cache_hash(cache)));
    HANDLE mutex=CreateMutexA(nullptr,FALSE,name);
    if(!mutex)throw std::runtime_error("could not create Windows cache commit mutex: "+std::system_category().message(int(GetLastError())));
    const DWORD waited=WaitForSingleObject(mutex,10000);
    if(waited!=WAIT_OBJECT_0&&waited!=WAIT_ABANDONED){
        const DWORD err=waited==WAIT_TIMEOUT?ERROR_TIMEOUT:GetLastError();
        CloseHandle(mutex);
        throw std::runtime_error("could not acquire Windows cache commit mutex: "+std::system_category().message(int(err)));
    }
    lease.mutex=mutex;
}
static void lrpk_pin_windows_writable(const fs::path&target){
    auto&lease=lrpk_windows_commit_lease;
    if(lease.pin!=INVALID_HANDLE_VALUE)throw std::runtime_error("Windows cache write pin was already held");
    const auto deadline=Clock::now()+std::chrono::seconds(5);
    for(;;){
        HANDLE pin=CreateFileW(target.c_str(),GENERIC_READ|GENERIC_WRITE,
            FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr);
        if(pin!=INVALID_HANDLE_VALUE){lease.pin=pin;return;}
        const DWORD err=GetLastError();
        if(err!=ERROR_SHARING_VIOLATION||Clock::now()>=deadline)
            throw std::runtime_error("cache is not writable after guarded cross-process rebuild: "+std::system_category().message(int(err)));
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
}
static bool lrpk_same_file(const fs::path&a,const fs::path&b){
    std::error_code ec;const bool same=fs::equivalent(a,b,ec);
    if(ec)throw std::runtime_error("guard identity check failed: "+ec.message());
    return same;
}
static void lrpk_recover_guard(const std::string&cache,bool force){
    const auto target=fs::u8path(cache),guard=lrpk_guard_path(cache);
    if(!fs::exists(guard))return;
    if(fs::exists(target)){
        if(!lrpk_same_file(target,guard))throw std::runtime_error("cache and recovery guard both exist with different contents; preserving both");
        log("GUARD_PRESENT\tfile="+cache);
        return;
    }
    if(force)return;
    const auto g=guard.u8string();
    if(lrpk_recover(g.c_str()))throw std::runtime_error("recovery guard WAL failed: "+error_text());
    fs::rename(guard,target);
    log("GUARD_RECOVERED\tfile="+cache);
}
static std::string lrpk_prepare_guarded_clear(PCM_source*inner,const char*media){
    const std::string cache=lrpk_cache_path_for_media(media);
    lrpk_recover_guard(cache,false);
    const auto target=fs::u8path(cache),guard=lrpk_guard_path(cache);
    if(fs::exists(target)){
        // Validate before creating a guard or asking REAPER to delete anything.
        // lrpk_read_standard performs the read-only standard/RPKX boundary scan;
        // malformed RPKX and unknown suffixes therefore fail while the original
        // canonical cache path is still untouched and visible to REAPER.
        Buffer validation;
        if(lrpk_read_standard(cache.c_str(),&validation.b))throw std::runtime_error("cache is not safely preservable; refusing native clear: "+error_text());
        if(fs::exists(guard)){
            if(!lrpk_same_file(target,guard))throw std::runtime_error("existing recovery guard differs from cache; refusing native clear");
        }else{
            fs::create_hard_link(target,guard);
            if(!fs::exists(guard)||!lrpk_same_file(target,guard))throw std::runtime_error("failed to verify recovery hard link; refusing native clear");
            log("GUARD_CREATED\tfile="+cache);
        }
    }
    // This is a clear/delete only. REAPER's native PeaksBuild_* is never called.
    Delegating d;inner->Peaks_Clear(true);
    // REAPER may choose a different default write directory once the original
    // filename disappears. Return the pre-clear path so the next plugin job
    // commits through the guard and republishes that exact sidecar name.
    return cache;
}
static std::string lrpk_commit_path(const std::string&cache){
    lrpk_acquire_windows_commit_lease(cache);
    const auto target=fs::u8path(cache),guard=lrpk_guard_path(cache);
    if(!fs::exists(guard)){
        // Another REAPER process may have just published a complete cache and
        // temporarily reopened it without FILE_SHARE_WRITE. Wait for that native
        // reader to release the file, then keep a share-all write handle open so
        // it cannot race back in between this probe and lrpk_replace().
        if(fs::exists(target))lrpk_pin_windows_writable(target);
        return cache;
    }
    const auto deadline=Clock::now()+std::chrono::seconds(5);
    while(fs::exists(target)){
        if(!lrpk_same_file(target,guard))throw std::runtime_error("guarded cache path was replaced by another file; preserving both");
        if(Clock::now()>=deadline)throw std::runtime_error("REAPER retained the guarded cache after native clear; original RPKX remains protected");
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    log("GUARD_DETACHED\tfile="+cache);
    return guard.u8string();
}
static void lrpk_finalize_guard(const std::string&cache,const std::string&commit){
    try{
        if(commit!=cache){
            const auto target=fs::u8path(cache),guard=fs::u8path(commit);
            if(fs::exists(target))throw std::runtime_error("cache reappeared before guarded commit could be published; preserving guard");
            fs::rename(guard,target);
            log("GUARD_COMMITTED\tfile="+cache);
        }
    }catch(...){lrpk_release_windows_commit_lease();throw;}
    lrpk_release_windows_commit_lease();
}
static void lrpk_restore_guard(const std::string&cache){
    try{
        const auto target=fs::u8path(cache),guard=lrpk_guard_path(cache);
        if(fs::exists(guard)){
            if(fs::exists(target)){
                if(!lrpk_same_file(target,guard))throw std::runtime_error("cannot restore guard because cache path contains a different file");
            }else{
                const auto g=guard.u8string();
                if(lrpk_recover(g.c_str()))throw std::runtime_error("guard recovery after failed generation failed: "+error_text());
                fs::rename(guard,target);
                log("GUARD_RESTORED\tfile="+cache);
            }
        }
    }catch(...){lrpk_release_windows_commit_lease();throw;}
    lrpk_release_windows_commit_lease();
}
#else
static void lrpk_recover_guard(const std::string&,bool){}
static std::string lrpk_prepare_guarded_clear(PCM_source*,const char*){return {};}
static std::string lrpk_commit_path(const std::string&cache){return cache;}
static void lrpk_finalize_guard(const std::string&,const std::string&){}
static void lrpk_restore_guard(const std::string&){}
#endif
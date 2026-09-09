-- Native-only long-source oracle for the cross-process gate. This script never
-- loads or calls the RPKX extension. It waits for REAPER's actual cache file to
-- contain the requested profile and become size-stable before exiting, rather
-- than assuming a fixed two-second build time.
local root=assert(os.getenv('LRPK_CASE'))
local media=assert(os.getenv('LRPK_MEDIA'))
local op=os.getenv('LRPK_ACTION') or 'manual'
local f=assert(io.open(root..'/result.txt','w'))
local closed=false
local function log(k,v) if not closed then f:write(k,'=',tostring(v),'\n');f:flush() end end
local function quit(err)
  if closed then return end
  if err then log('error',err) end
  pcall(reaper.Main_SaveProjectEx,0,root..'/saved.rpp',0)
  log('finished',true);f:close();closed=true;reaper.Main_OnCommand(40004,0)
end
local function normalize(s) return tostring(s or ''):gsub('%z',''):gsub('%s+',' '):match('^%s*(.-)%s*$'):lower() end
local function actions()
  local by_name={}
  for i=0,65535 do
    local id=reaper.kbd_enumerateActions(0,i)
    if not id or id<=0 then break end
    local name=reaper.kbd_getTextFromCmd(id,0) or ''
    by_name[normalize(name)]=id
  end
  return by_name
end
local function i32le(s,pos)
  local a,b,c,d=s:byte(pos,pos+3)
  if not d then return nil end
  local u=a+b*256+c*65536+d*16777216
  if u>=2147483648 then u=u-4294967296 end
  return u
end
local function cache_profile(path)
  local h=io.open(path,'rb');if not h then return nil,nil end
  local size=h:seek('end');h:seek('set',0)
  local head=h:read(512) or '';h:close()
  if #head<18 then return size,nil end
  local magic=head:sub(1,4)
  if magic~='RPKN' and magic~='RPKL' then return size,nil end
  local layers=head:byte(6) or 0
  if #head<18+8*layers then return size,nil end
  local has_spectrogram=false
  for i=0,layers-1 do
    if i32le(head,19+i*8)==-103 then has_spectrogram=true end
  end
  return size,has_spectrogram
end
local function source()
  local item=reaper.GetMediaItem(0,0);if not item then return nil end
  local take=reaper.GetActiveTake(item);if not take then return nil end
  local src=reaper.GetMediaItemTake_Source(take)
  for i=1,16 do local p=reaper.GetMediaSourceParent(src);if not p then break end;src=p end
  return src
end
local function main()
  log('version',reaper.GetAppVersion());log('resource',reaper.GetResourcePath());log('plugin',reaper.APIExists('RPKX_Status'))
  if reaper.APIExists('RPKX_Status') then error('native oracle unexpectedly loaded reference extension') end
  local by_name=actions()
  local function action(names,candidates)
    local id
    for _,name in ipairs(names) do if by_name[normalize(name)] then id=by_name[normalize(name)];break end end
    if not id then
      for _,candidate in ipairs(candidates or {}) do
        local actual=normalize(reaper.kbd_getTextFromCmd(candidate,0))
        for _,name in ipairs(names) do if actual==normalize(name) then id=candidate;break end end
        if id then break end
      end
    end
    if not id then error('Missing host action: '..table.concat(names,' / ')) end
    log('action',reaper.kbd_getTextFromCmd(id,0));log('action_id',id);reaper.Main_OnCommand(id,0);log('action_returned',id)
  end
  log('phase','import_begin');reaper.InsertMedia(media,0);log('phase','import_returned')
  local started=reaper.time_precise();local acted=false;local action_time=started
  local last_size=-1;local stable_since=nil
  local tick
  tick=function()
    local ok,err=xpcall(function()
      local now=reaper.time_precise();local age=now-started;local src=source()
      if not src then if age>12 then error('No imported source') end;reaper.defer(tick);return end
      if not acted and age>1 then
        acted=true
        if op=='manual' then action({'Peaks: Rebuild all peaks','Peaks: Rebuild peaks'},{40048,41101})
        elseif op=='spectrogram' then action({'Peaks: Toggle spectrogram'},{42294})
        else error('unsupported native oracle operation: '..op) end
        action_time=reaper.time_precise();reaper.defer(tick);return
      end
      reaper.UpdateArrange()
      if acted then
        local write=reaper.GetPeakFileNameEx(media,'',true) or ''
        local read=reaper.GetPeakFileNameEx(media,'',false) or ''
        local path=write~='' and write or read
        local size,has_spectrogram=cache_profile(path)
        local profile_ready=size and size>0 and ((op=='spectrogram' and has_spectrogram==true) or (op=='manual' and has_spectrogram==false))
        if profile_ready then
          if size~=last_size then last_size=size;stable_since=now
          elseif stable_since and now-stable_since>0.75 then
            local arr=reaper.new_array(64);local n=reaper.PCM_Source_GetPeaks(src,100,0,2,16,0,arr)
            if (n & 0xfffff)>0 then
              log('type',reaper.GetMediaSourceType(src,''));log('final_status',0);log('peak_read',read);log('peak_write',write)
              log('peak_count',n & 0xfffff);log('cache_bytes',size);quit();return
            end
          end
        else
          last_size=-1;stable_since=nil
        end
      end
      if age>80 then error('native oracle cache did not reach requested stable profile') end
      reaper.defer(tick)
    end,debug.traceback)
    if not ok then quit(err) end
  end
  reaper.defer(tick)
end
local ok,err=xpcall(main,debug.traceback)
if not ok then quit(err) end

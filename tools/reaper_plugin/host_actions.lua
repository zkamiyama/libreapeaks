-- Test driver: ordinary host actions only. Never call PCM_Source_BuildPeaks
-- or RPKX_ForceBuild. RPKX_Status is an observation, not a build trigger.
local root = assert(os.getenv('LRPK_CASE'))
local media = assert(os.getenv('LRPK_MEDIA'))
local op = os.getenv('LRPK_ACTION') or 'import'
local plugin = os.getenv('LRPK_EXPECT_PLUGIN') == '1'
local f = assert(io.open(root .. '/result.txt', 'w'))
local function log(k,v) f:write(k,'=',tostring(v),'\n'); f:flush() end
log('version',reaper.GetAppVersion())
log('resource',reaper.GetResourcePath())
log('plugin',reaper.APIExists('RPKX_Status'))
local function quit(err)
  if err then log('error',err) end
  reaper.Main_SaveProjectEx(0,root .. '/saved.rpp',0)
  log('finished',true); f:close()
  reaper.Main_OnCommand(40004,0)
end
local actions={}
local section=reaper.SectionFromUniqueID(0)
for i=0,65535 do
  local id,name=reaper.kbd_enumerateActions(section,i)
  if id==0 then break end
  actions[name]=id
end
local af=assert(io.open(root..'/actions.txt','w'))
for name,id in pairs(actions) do
  if name:find('peak') or name:find('Peak') or name:find('offline') or name:find('online') or name:find('Reverse') then af:write(id,'\t',name,'\n') end
end
af:close()
local function action(name)
  local id=actions[name]; if not id then error('Missing host action: '..name) end
  log('action',name); reaper.Main_OnCommand(id,0)
end
local function source()
  local item=reaper.GetMediaItem(0,0); if not item then return nil end
  local take=reaper.GetActiveTake(item); if not take then return nil end
  local src=reaper.GetMediaItemTake_Source(take)
  for i=1,16 do local parent=reaper.GetMediaSourceParent(src); if not parent then break end; src=parent end
  return src
end
local initial=reaper.time_precise()
local acted=false
if op=='project' then reaper.Main_openProject(root..'/input.rpp') else reaper.InsertMedia(media,0) end
local last=-999
local function tick()
  local ok,err=xpcall(function()
    local src=source(); if not src then if reaper.time_precise()-initial>5 then error('No imported source') end; reaper.defer(tick); return end
    local st=plugin and reaper.APIExists('RPKX_Status') and reaper.RPKX_Status(src) or 0
    if st~=last then log('status',st);last=st end
    local age=reaper.time_precise()-initial
    if not acted and age>1.0 and (not plugin or st==2 or st==0 or st==-1 or st==-2) then
      acted=true
      if op=='manual' then action('Peaks: Rebuild all peaks')
      elseif op=='selected' then action('Peaks: Rebuild peaks for selected items')
      elseif op=='spectrogram' then action('Peaks: Toggle spectrogram')
      elseif op=='reverse' then action('Item properties: Toggle take reverse'); action('Peaks: Rebuild all peaks')
      elseif op=='online' then action('Item: Set all media offline'); action('Item: Set all media online')
      end
    end
    reaper.UpdateArrange()
    if age>3 and acted and (not plugin or st==2 or st==-1 or st==-2) then
      log('type',reaper.GetMediaSourceType(src,''))
      log('final_status',st)
      log('peak_read',reaper.GetPeakFileNameEx(media,'',false))
      log('peak_write',reaper.GetPeakFileNameEx(media,'',true))
      local arr=reaper.new_array(64)
      local n=reaper.PCM_Source_GetPeaks(src,100,0,2,16,0,arr)
      log('peak_count',n & 0xfffff)
      quit(); return
    end
    if age>40 then error('Host scheduler timeout status='..st) end
    reaper.defer(tick)
  end,debug.traceback)
  if not ok then quit(err) end
end
reaper.defer(tick)

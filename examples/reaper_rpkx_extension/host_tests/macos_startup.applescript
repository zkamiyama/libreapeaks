-- Test-only UI automation. Scope to the REAPER PID started by this case.
-- Only inspect the two known small startup dialogs. In particular, never walk
-- the accessibility tree of REAPER's main/Building Peaks windows: doing so can
-- block System Events long enough to break the shared-race startup barrier.
on run argv
    set targetPID to (item 1 of argv) as integer
    set reportText to ""
    tell application "System Events"
        set candidates to every application process whose unix id is targetPID
        if (count of candidates) is 0 then return "no process"
        tell item 1 of candidates
            repeat with win in windows
                try
                    set winName to name of win as text
                    set reportText to reportText & "WINDOW " & winName & linefeed
                    set isAudioDialog to (winName is "REAPER")
                    set isEvaluationDialog to (winName contains "EVALUATION LICENSE" or winName starts with "About REAPER")

                    if isAudioDialog or isEvaluationDialog then
                        -- These modal trees are tiny. A recursive AX walk is
                        -- bounded to these known dialogs only, which preserves
                        -- the reliable button discovery of the original harness
                        -- without ever traversing large live REAPER windows.
                        set elementsList to entire contents of win
                        set audioPrompt to false
                        repeat with el in elementsList
                            try
                                set roleText to role of el as text
                                set nameText to name of el as text
                                set reportText to reportText & roleText & " " & nameText & linefeed
                                if isAudioDialog and roleText is "AXStaticText" and nameText contains "audio device" then
                                    set audioPrompt to true
                                end if
                            end try
                        end repeat
                        repeat with el in elementsList
                            try
                                set roleText to role of el as text
                                set nameText to name of el as text
                                if roleText is "AXButton" and enabled of el then
                                    if isEvaluationDialog and nameText contains "Still Evaluating" then
                                        click el
                                        set reportText to reportText & "CLICKED " & nameText & linefeed
                                        exit repeat
                                    else if isAudioDialog and audioPrompt and nameText is "No" then
                                        click el
                                        set reportText to reportText & "CLICKED " & nameText & linefeed
                                        exit repeat
                                    end if
                                end if
                            end try
                        end repeat
                    end if
                on error messageText
                    set reportText to reportText & "ERROR " & messageText & linefeed
                end try
            end repeat
        end tell
    end tell
    return reportText
end run

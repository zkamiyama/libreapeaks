-- Test-only UI automation. Scope to the REAPER PID started by this case.
-- Do not modify registration, binary signatures, or system privacy settings.
on run argv
    set targetPID to (item 1 of argv) as integer
    set reportText to ""
    tell application "System Events"
        set candidates to every application process whose unix id is targetPID
        if (count of candidates) is 0 then return "no process"
        tell item 1 of candidates
            repeat with win in windows
                try
                    set reportText to reportText & "WINDOW " & (name of win as text) & linefeed
                    set elementsList to entire contents of win
                    set audioPrompt to false
                    repeat with el in elementsList
                        try
                            set roleText to role of el as text
                            set nameText to name of el as text
                            set reportText to reportText & roleText & " " & nameText & linefeed
                            if roleText is "AXStaticText" and nameText contains "audio device" then
                                set audioPrompt to true
                            end if
                        end try
                    end repeat
                    repeat with el in elementsList
                        try
                            set roleText to role of el as text
                            set nameText to name of el as text
                            if roleText is "AXButton" and enabled of el then
                                if nameText contains "Still Evaluating" then
                                    click el
                                    set reportText to reportText & "CLICKED " & nameText & linefeed
                                else if audioPrompt and nameText is "No" then
                                    click el
                                    set reportText to reportText & "CLICKED " & nameText & linefeed
                                end if
                            end if
                        end try
                    end repeat
                on error messageText
                    set reportText to reportText & "ERROR " & messageText & linefeed
                end try
            end repeat
        end tell
    end tell
    return reportText
end run
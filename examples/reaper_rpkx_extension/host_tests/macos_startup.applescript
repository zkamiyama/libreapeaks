-- Test-only UI automation. Scope to the REAPER PID started by this case.
-- Handle only the two known startup dialogs. Avoid `entire contents`, because
-- walking REAPER's full accessibility tree can block System Events long enough
-- for the cross-process barrier to time out on hosted macOS runners.
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

                    -- Fresh isolated REAPER roots may ask for an audio device.
                    -- This is a known modal dialog whose top-level buttons are
                    -- directly exposed, so no recursive AX traversal is needed.
                    if winName is "REAPER" then
                        repeat with btn in buttons of win
                            try
                                set btnName to name of btn as text
                                set reportText to reportText & "AXButton " & btnName & linefeed
                                if btnName is "No" and enabled of btn then
                                    click btn
                                    set reportText to reportText & "CLICKED " & btnName & linefeed
                                    exit repeat
                                end if
                            end try
                        end repeat

                    -- Evaluation startup window. Prefer the explicit button when
                    -- Accessibility exposes its label. If it is temporarily
                    -- unlabeled during the countdown, Escape is safe because the
                    -- window identity is already pinned to REAPER's About dialog.
                    else if winName starts with "About REAPER" then
                        set dismissed to false
                        repeat with btn in buttons of win
                            try
                                set btnName to name of btn as text
                                set reportText to reportText & "AXButton " & btnName & linefeed
                                if btnName contains "Still Evaluating" and enabled of btn then
                                    click btn
                                    set dismissed to true
                                    set reportText to reportText & "CLICKED " & btnName & linefeed
                                    exit repeat
                                end if
                            end try
                        end repeat
                        if not dismissed then
                            key code 53
                            set reportText to reportText & "KEY Escape" & linefeed
                        end if
                    end if
                on error messageText
                    set reportText to reportText & "ERROR " & messageText & linefeed
                end try
            end repeat
        end tell
    end tell
    return reportText
end run

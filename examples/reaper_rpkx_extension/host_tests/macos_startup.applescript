-- Test-only UI automation. Scope to the REAPER PID started by this case.
-- Never traverse REAPER's accessibility tree: even a modal's recursive AX walk
-- can block System Events long enough to break the host/race startup deadline.
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
                    -- These buttons are exposed directly on the top-level window,
                    -- so bounded direct lookup is sufficient and non-blocking.
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

                    -- The evaluation modal's "Still Evaluating" control is not
                    -- reliably exposed as a top-level AXButton on hosted macOS.
                    -- Once the countdown expires it is the default action, so
                    -- Return dismisses it without any recursive AX traversal.
                    else if winName contains "EVALUATION LICENSE" or winName starts with "About REAPER" then
                        key code 36
                        set reportText to reportText & "KEY Return" & linefeed
                    end if
                on error messageText
                    set reportText to reportText & "ERROR " & messageText & linefeed
                end try
            end repeat
        end tell
    end tell
    return reportText
end run

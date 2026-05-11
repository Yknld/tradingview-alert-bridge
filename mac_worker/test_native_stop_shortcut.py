import subprocess
import time


APP_NAME = "Chromium"


APPLESCRIPT = """
tell application "Chromium" to activate
delay 0.25
tell application "System Events"
    key code 125
    delay 0.10
    key code 125
    delay 0.10
    key code 125
    delay 0.10
    key code 36
end tell
"""


def main() -> None:
    print("Native STOP shortcut test", flush=True)
    print("1. Open Tradovate", flush=True)
    print("2. Open the ORDER TYPE dropdown manually", flush=True)
    print("3. Leave the dropdown open and focused", flush=True)
    print("4. You have 3 seconds before keys are sent", flush=True)
    time.sleep(3)

    completed = subprocess.run(
        ["osascript", "-e", APPLESCRIPT],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    print(f"returncode={completed.returncode}", flush=True)
    if completed.stdout.strip():
        print("stdout:", completed.stdout.strip(), flush=True)
    if completed.stderr.strip():
        print("stderr:", completed.stderr.strip(), flush=True)
    print("Done.", flush=True)


if __name__ == "__main__":
    main()

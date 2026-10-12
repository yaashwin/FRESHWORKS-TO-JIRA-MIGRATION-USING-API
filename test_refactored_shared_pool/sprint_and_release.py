import subprocess
import sys
 
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
from threading import Thread, Lock

SCRIPTS = ["extract_releases.py", "extract_sprints.py" , "extract_project_details.py"]
output_lock = Lock()


def run_script(script_name):
    try:
        process = subprocess.Popen(
            [sys.executable, script_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        for line in process.stdout:
            line = line.rstrip()
            if line:
                with output_lock:
                    print(f"[{script_name}] {line}")

        return_code = process.wait()

        with output_lock:
            status = "OK" if return_code == 0 else f"FAIL ({return_code})"
            print(f"[{script_name}] finished → {status}")

    except Exception as e:
        with output_lock:
            print(f"[{script_name}] error → {e}")


def main():
    threads = []

    for script in SCRIPTS:
        t = Thread(target=run_script, args=(script,))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
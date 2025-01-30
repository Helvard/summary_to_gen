import time
import os
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from colorama import init, Fore, Style
import sys

init()  # Initialize colorama

class LogHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith('genesys_integration.log'):
            with open(event.src_path, 'r') as f:
                # Read the last line of the file
                lines = f.readlines()
                if lines:
                    last_line = lines[-1].strip()
                    # Color-code based on log level
                    if 'ERROR' in last_line:
                        print(f"{Fore.RED}{last_line}{Style.RESET_ALL}")
                    elif 'WARNING' in last_line:
                        print(f"{Fore.YELLOW}{last_line}{Style.RESET_ALL}")
                    elif 'INFO' in last_line:
                        print(f"{Fore.GREEN}{last_line}{Style.RESET_ALL}")
                    else:
                        print(f"{Fore.CYAN}{last_line}{Style.RESET_ALL}")

def watch_logs():
    # Create logs directory if it doesn't exist
    os.makedirs('logs', exist_ok=True)
    
    # Set up the observer
    observer = Observer()
    observer.schedule(LogHandler(), 'logs', recursive=False)
    observer.start()

    print(f"{Fore.GREEN}Starting log viewer...{Style.RESET_ALL}")
    print(f"{Fore.CYAN}Watching logs/genesys_integration.log{Style.RESET_ALL}")
    print("Press Ctrl+C to stop")

    try:
        # Print existing log content
        log_file = 'logs/genesys_integration.log'
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                content = f.read()
                print(f"{Fore.CYAN}=== Existing Log Content ==={Style.RESET_ALL}")
                print(content)
                print(f"{Fore.CYAN}=== End of Existing Content ==={Style.RESET_ALL}")

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print(f"{Fore.YELLOW}Stopping log viewer...{Style.RESET_ALL}")

    observer.join()

if __name__ == "__main__":
    watch_logs()

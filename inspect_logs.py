import os


def tail_logs(filename, lines=50):
    """Reads the last N lines of a file without loading the whole file into memory."""
    if not os.path.exists(filename):
        print(f"Error: File '{filename}' not found in this directory.")
        print("Available files:", [f for f in os.listdir('.') if f.endswith('.log') or f.endswith('.txt')])
        return

    with open(filename, 'rb') as f:
        try:
            f.seek(0, os.SEEK_END)
            buffer = bytearray()
            pointer = f.tell()
            count = 0

            while pointer > 0 and count < lines:
                pointer -= 1
                f.seek(pointer)
                char = f.read(1)
                if char == b'\n':
                    count += 1
                buffer.extend(char)

            # Reverse and print
            print(buffer[::-1].decode('utf-8', errors='ignore'))
        except Exception as e:
            print(f"Could not read file: {e}")


if __name__ == "__main__":
    # If your log file is named something else, change 'server.log' below
    log_name = 'server.log'

    print(f"--- Viewing last 50 lines of {log_name} ---")
    tail_logs(log_name)
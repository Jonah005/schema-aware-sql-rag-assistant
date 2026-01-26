log_file = 'output.log'
target = "Internal Server Error: /api/chat/"

with open(log_file, 'r') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if target in line:
        print("\n" + "=" * 50)
        print("FOUND CRASH LOG:")
        # Print the 15 lines after the error where the Traceback lives
        for j in range(i, min(i + 20, len(lines))):
            print(lines[j].strip())
        print("=" * 50)
import os


def print_tree(path, prefix=''):
    entries = sorted(os.listdir(path))
    for idx, name in enumerate(entries):
        # 1) Skip .venv
        if name == '.venv' or name == ".git":
            continue

        full = os.path.join(path, name)
        connector = '└── ' if idx == len(entries)-1 else '├── '
        print(prefix + connector + name)
        if os.path.isdir(full):
            extension = '    ' if idx == len(entries)-1 else '│   '
            print_tree(full, prefix + extension)

if __name__ == '__main__':
    root = r'C:\Users\Brannon Luong\Desktop\Smart-Parking-Lot-Monitoring-main'
    print(root)
    print_tree(root)
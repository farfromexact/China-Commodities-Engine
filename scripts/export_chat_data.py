"""Refresh the offline Chat export from existing data; never connect to Wind."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from china_commodities.chat_export import export_chat_data


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    export_chat_data(root / 'data')
    print(root / 'data/chat/manifest.json')

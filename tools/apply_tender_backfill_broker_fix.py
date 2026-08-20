from __future__ import annotations

from pathlib import Path

BROKER = Path('tools/global_capacity_broker_v3.py')
OLD = 'TENDER_REPO = "spmbiz/tender-engine"'
NEW = 'TENDER_REPO = "walidgdg1-ai/tender-engine"'


def main() -> None:
    text = BROKER.read_text(encoding='utf-8')
    if NEW in text:
        print('Tender repo broker fix already applied')
    elif OLD in text:
        text = text.replace(OLD, NEW, 1)
        BROKER.write_text(text, encoding='utf-8')
        print('Updated global broker Tender repo to walidgdg1-ai/tender-engine')
    else:
        raise SystemExit('Expected Tender repo marker missing; refusing blind patch')

    check = BROKER.read_text(encoding='utf-8')
    assert NEW in check
    assert OLD not in check


if __name__ == '__main__':
    main()

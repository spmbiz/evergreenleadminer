#!/usr/bin/env python3
from pathlib import Path
import shutil
import harvest

# Run only the 12 public figures that failed the first pass.
shutil.copyfile('retry12.json', 'people100.json')
harvest.ROOT = Path('people-photos-retry12')
harvest.MIN = 650

# Keep the desired five-view structure, but broaden each slot so an unavailable
# exact angle can be replaced by another clearly distinct, relevant photo.
harvest.TYPES = [
    ('portrait-front', [
        'official portrait', 'headshot portrait', 'close up interview',
        'podcast guest photo', 'press portrait high resolution'
    ]),
    ('three-quarter', [
        'three quarter portrait', 'red carpet event', 'award event photo',
        'interview candid', 'press photo high resolution'
    ]),
    ('profile-side', [
        'side profile', 'speaking side view', 'candid event',
        'interview photo', 'on stage photo'
    ]),
    ('full-body', [
        'full body standing', 'full length event', 'walking event',
        'standing on stage', 'red carpet full length'
    ]),
    ('context-event', [
        'event interview stage', 'podcast interview', 'speaking on stage',
        'public appearance', 'candid professional photo'
    ]),
]

# More candidates for hard-to-find internet personalities.
_original_search = harvest.search

def expanded_search(query, tokens):
    rows = _original_search(query, tokens)
    if len(rows) < 20:
        # Alias/context-light fallback while retaining metadata identity scoring.
        simple = query.replace(' high resolution', '').replace(' photo', '')
        rows += [r for r in _original_search(simple, tokens)
                 if r.get('image') not in {x.get('image') for x in rows}]
    return rows

harvest.search = expanded_search
harvest.main()

src = Path('people-photos-100.zip')
dst = Path('people-photos-retry12.zip')
if src.exists():
    src.replace(dst)

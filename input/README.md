# input/

Place an Altium schematic library (`.SchLib`) here to work with it.

`.SchLib` files are **git-ignored** (see `.gitignore`) because they may contain
proprietary component data — they are intentionally not committed to this
repository.

The acceptance program picks up a single `.SchLib` in this folder
automatically:

```bash
python list_components.py
```

or pass an explicit path from anywhere:

```bash
python list_components.py path/to/library.SchLib
```

Tests that need a sample library skip automatically when this folder has none.

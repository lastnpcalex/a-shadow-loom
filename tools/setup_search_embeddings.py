"""Explicit model installation, never invoked by a search or a server restart."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exo_state import state_path
from search_space.embeddings import MODEL


def main():
    from fastembed import TextEmbedding
    root = state_path("search-space", "models")
    model = TextEmbedding(MODEL, cache_dir=str(root), threads=2)
    vector = next(model.embed(["Where did we discuss conversation recovery?"]))
    print(f"Local search model ready: {MODEL}; dimensions={len(vector)}; cache={root}")


if __name__ == "__main__":
    main()

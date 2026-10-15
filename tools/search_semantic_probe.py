"""Real local embedding acceptance on synthetic text only; no Loom DB access."""
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from search_space.index import SearchIndex


def main():
    with tempfile.TemporaryDirectory(prefix="loom-search-semantic-") as folder:
        index = SearchIndex(Path(folder) / "search.sqlite3")
        workspace = Path(folder) / "synthetic"
        key = index.set_policy(workspace, "cloud")["workspace_key"]
        documents = [
            (1, "Recovery", "We preserve backups of earlier files to restore accidentally overwritten work."),
            (2, "Astronomy", "The planet follows an elliptical orbit around its star."),
            (3, "UI", "We changed the chat message font and button colors."),
        ]
        index.sync_workspace(workspace, [{"message_id": mid, "conversation_id":mid,
            "conversation_title": title, "role":"assistant", "created_at":mid, "content":text}
            for mid,title,text in documents])
        started = time.perf_counter()
        query = "undo unintended modifications"
        assert index.search([query], [key]) == [], "Probe must require semantic retrieval"
        result = index.hybrid_search(query, [key])
        assert result["retrieval_mode"] == "semantic+keyword", result
        assert result["sources"][0]["message_id"] == 1, result
        cold = time.perf_counter() - started
        started = time.perf_counter()
        again = index.hybrid_search(query, [key])
        assert again["sources"][0]["message_id"] == 1
        print(json.dumps({"verified":True,"model":index.embeddings.model,"synthetic_documents":3,
                          "cold_seconds":round(cold,3),"warm_seconds":round(time.perf_counter()-started,3)}))


if __name__ == "__main__":
    main()

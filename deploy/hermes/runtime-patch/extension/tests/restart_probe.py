"""Offline acceptance probe; requires a disposable home containing the preserved clients."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, "/opt/durable-video")
from inbox import Inbox, load_workflow


def envelope():
    urls = ["https://v.douyin.com/WrSLMd2PqLw/", "https://v.douyin.com/djQguBR2XHw/"]
    return {"profile": "default", "bot_id": "acceptance-only", "chat_id": "acceptance-only",
            "message_id": "restart-probe", "text": " ".join(urls), "urls": urls}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("receive", "process", "verify"))
    args = parser.parse_args()
    home = Path("/opt/data")
    if not (home / ".disposable-restart-probe").is_file():
        raise RuntimeError("refusing acceptance probe outside a marked disposable home")
    store = Inbox(home)
    if args.mode == "receive":
        store.receive(envelope())
        assert len(store.requests()) == 1 and not store.jobs()
    elif args.mode == "process":
        module, workflow = load_workflow(home)
        assert len(store.jobs()) == 2

        class Backend:
            def capture(self, item, save):
                media = workflow.directory(item["id"]) / "synthetic-source.mp4"
                media.write_bytes(b"synthetic workflow handoff fixture; no media decoding")
                save(capture_job_id="probe-" + item["id"])
                return {"file": str(media), "canonical_url": item["original_url"]}

            def process(self, item):
                return {"status": "complete", "transcription": {"status": "complete"},
                        "visual_analysis": {"status": "complete"}}

        workflow.run_pending(Backend(), limit=2)
        store.inventory(workflow)
    else:
        assert all(row["submitted"] == 1 for row in store.requests())
        before = store.jobs()
        assert len(before) == 2
        store.receive(envelope())
        module, workflow = load_workflow(home)
        assert store.submit_pending(workflow) == 0
        assert store.jobs() == before
        for row in before:
            item = workflow.status(row["item_id"])
            assert item["state"] == "completed"
            assert (workflow.directory(row["item_id"]) / ".durable-owned.json").is_file()
    print(json.dumps({"stage": args.mode, "requests": len(store.requests()), "jobs": len(store.jobs())}))


if __name__ == "__main__":
    main()

# Loom Thread and Run Routing Invariants

These are correctness rules, not UI preferences. Changes to messages,
generation launch, reconnect, queues, tree navigation, or WebSockets must keep
all of them true.

1. Every persisted message belongs to exactly one `loom_thread_id`.
2. A Loom thread has at most one root message. A standalone `+ Thread` action
   creates the empty server thread before the user types its first message.
3. Every browser page has its own server cursor: conversation, Loom thread,
   exact leaf, and monotonic revision.
4. A user-message write supplies that complete cursor contract. The server
   compare-and-swaps the revision and rejects stale, missing, cross-thread, or
   cross-conversation targets. It never infers a parent from `is_active`,
   focus, visibility, a WebSocket, or another page.
5. An ordinary Send persists the user message and starts (or durably queues)
   its generation in one server request. A generation is bound to that
   persisted parent message and Loom thread before the provider starts.
   Browser reconnect state cannot replay a free-floating launch into another
   conversation, and provider output cannot choose or change its destination.
6. Generated content is run-scoped. Only exact `run_id` subscriptions receive
   tokens, tools, permissions, or progressive snapshots. There is no
   conversation-wide content fallback and subscribing never transfers run
   provenance from one page to another.
7. A page may subscribe when its durable cursor is on that run's exact parent,
   draft, or queued continuation in the same Loom thread. Focus and branch
   visibility alone are not authorization.
8. Backgrounding, disconnecting, reconnecting, and returning to a page only
   reconcile that page's server cursor. They do not select or advance another
   thread. Unsent text, attachments, and reply quotes are saved per page cursor
   rather than carried into a different conversation or branch.
9. Queued continuations are durable server rows bound to their Loom thread.
   Queue state returned to a page is filtered to that page's thread.
10. Global notifications contain metadata only. Child-agent activity remains
    outside `messages` unless an explicit attach operation is requested.

Regression coverage lives in `tests/test_thread_topology.py`,
`tests/test_event_router.py`, `tests/test_parallel_agents.py`, and
`tests/test_tree_send_static.py`.

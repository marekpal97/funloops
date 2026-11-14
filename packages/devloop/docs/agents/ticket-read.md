# The ticket read — one recipe

`issue-loop.command.md` §0 runs this recipe for each frontier entry whose
`ticket_read.exists` is false, before the claim. A judge with zero context
reads the ticket against the code and answers two questions: can I build
this without a guess, and can I run each demo as written.

`plan` computes `ticket_read` per frontier issue. `marker` is the first line
of the read's comment for the ticket's current body: it carries a digest of
that body. `exists` says whether a comment on the issue holds that line. A
changed body has a new digest, so it gets a new read.

1. Pack the read into the run directory, from the main checkout:

   ```bash
   uv run devloop pack <N> --role judge --posture ticket [--decisions <file>] \
     --return <run-dir>/ticket-<N>.json --out <run-dir>/dispatch-ticket-<N>.md
   ```

   The pack holds the ticket, its parent epic, the cited decisions, the repo
   map and the ticket brief. Add nothing to it: the reader must know only
   what the implementer will know. The pack refuses `--prime`, `--trace` and
   `--report`.
2. Dispatch it to a fresh agent as `dispatch.judge` says (§1b), never to a
   fork of your session.
3. Validate the return:

   ```bash
   uv run devloop validate --gate judge --posture ticket --return-json <run-dir>/ticket-<N>.json
   ```

   Exit `2` is a schema rejection: re-ask the same agent once with the
   printed `reasons` and a fresh return path. A second rejection routes the
   issue to human.
4. Comment on the ticket. The first line is `ticket_read.marker`, exactly as
   `plan` printed it. Then one bullet per finding: its class, the quoted
   line and the evidence, verbatim. With no finding, write `No finding.`
5. Route by the exit code:
   - Exit `1` (`needs_info` is true: a `false-premise` or `demo-cannot-run`
     finding): swap `ready-for-agent` for `needs-info` and dispatch nothing
     for the issue.
   - Exit `0`: the issue stays in the frontier. A `guess` finding is
     advisory; the comment is its whole effect.

**Host extension: the cited decisions.** Where the host has a Thinkweave
vault, write the full text of each decision under the ticket's `## Decisions`
heading to a file (`weave_read`, copied verbatim) and pass it as
`--decisions`. Without the file the pack marks the decision text as not
supplied.

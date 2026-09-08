<!--
  devloop's packaged constitution: what holds in the code, for any repo devloop
  installs into. `devloop pack` renders it as the `## Rules` section of both
  roles' packs, so the judge cites a rule by number. A repo extends it with
  its own docs/agents/constitution.md (cli.find_constitution).

  A rule is packaged only if it holds for any repo and names, in plain words,
  the failure it prevents. The rule text carries no citation; the PR that adds
  a rule cites the vault decision behind it. A rule enters a repo's overlay by
  a human's PR after it has bitten more than once, and moves here only when a
  second repo's overlay carries it too. The loop never writes to either layer.
  One screen, at most eight rules. A rule leaves when it becomes a
  deterministic check. These rules are about the code; rules about how the
  loop runs live in the loop's own doc.
-->

# The constitution

1. **Deepen before you spread.** Extend the module you are in before you add a neighbour. A module with one consumer belongs inside that consumer. A codebase made of many small modules is harder to navigate than one made of a few deep ones.

2. **Compose before you build.** Before you write anything, check whether the system already has it: a CLI verb, a tool, a hook, a sibling package. Use it. If you borrow a pattern from elsewhere, say why its constraints hold here. A parallel re-implementation is the most expensive diff that still passes tests.

3. **Objects carry the logic.** Model the domain as a few objects with the right methods and parameters, so the logic reads from the call site. Do not thread the same arguments through a chain of free functions. Do not add passive dataclasses or one-off classes as a habit. A record that crosses a function boundary travels as one declared record type, not as a bare string, tuple, or dict. That type is a surface: add it once, at the boundary, and reuse it.

4. **Key logic on top, plumbing below.** A module reads top-down. The substance is short and exposed at the top. Parsing, path handling, header stripping and other mundane work sit lower, behind small names, and a reader can follow the logic without reading them.

5. **Agents own judgement; code owns the tooling.** In a system that composes code with agent judgement, deciding what something means is the agent's job: whether a message is feedback, whether a criterion is met, what an issue needs. Never a regex, a score, or a threshold standing in for that decision. Code owns what lets the agent act: CLI verbs, input and output schemas, gates, sequencing, and the parsing of declared shapes. Everything reproducible is code. Nothing interpretive is.

6. **Never fail open.** A degraded path announces itself. An error swallowed into a clean, empty result is a bug, not a fallback.

7. **Contracts are declared, not narrated.** A convention stated in prose gets a schema, a declaration, or a test. Data another stage consumes travels as a declared shape, never as prose the next stage re-parses. Siblings share one signature; a special case at the call site is a defect. Docstrings say what; decisions say why.

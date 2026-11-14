# Third-party notices

This inventory records the origin and licence of bundled material, taken from the files themselves and from the upstream repositories named below (checked 2026-11-14). An unspecified licence is not a grant of redistribution rights.

- `skills/benchmark-checklist`, `skills/blast-radius`, `skills/correct`: adapted from pstack by Lauren Tan, MIT. Licence notices are in each directory.
- `skills/thermo-nuclear-code-quality-review`: adapted from Cursor's cursor-team-kit, MIT; licence in its directory.
- `skills/code-reviewer`, `skills/nextjs-developer`, `skills/python-pro`, `skills/react-expert`, `skills/test-master`, `skills/typescript-pro`: Jeffallan/claude-skills (github.com/Jeffallan/claude-skills), MIT; the upstream licence is in each directory. `code-reviewer` and `test-master` also contain material attributed in-file to obra/superpowers by Jesse Vincent, MIT.
- `skills/brainstorming`, `skills/dispatching-parallel-agents`, `skills/executing-plans`, `skills/systematic-debugging`, `skills/using-git-worktrees`, `skills/verification-before-completion`, `skills/writing-plans`, `skills/writing-skills`: obra/superpowers by Jesse Vincent (github.com/obra/superpowers), MIT; the upstream licence is in each directory. The kit changes two diagnostic lines in `systematic-debugging` so that they report whether a variable is set without printing its value, and one line in `using-git-worktrees` so that the agent asks before running a package install.
- `skills/recall`: arjunkmrm/recall, MIT; licence in its directory; the kit changes its storage and instructions.
- `skills/typesafe-ai`: TypeSafe AI, MIT; licence in its directory.
- `skills/impeccable`: pbakaus/impeccable (github.com/pbakaus/impeccable), Apache 2.0, based on Anthropic's frontend-design skill. The upstream `LICENSE` and `NOTICE.md` are in its directory. The kit's copy is modified: the live-preview mode and a clean-up script are removed.
- `skills/design-taste-frontend`, `skills/high-end-visual-design`: Leonxlnx/taste-skill (github.com/Leonxlnx/taste-skill), MIT; the upstream licence is in each directory.
- `skills/extract-design`: Manavarya09/design-extract (github.com/Manavarya09/design-extract), MIT; the upstream licence is in its directory.
- `skills/firecrawl-map`, `skills/firecrawl-scrape`, `skills/firecrawl-search`: firecrawl/cli (github.com/firecrawl/cli); its `package.json` states ISC. The repository carries no separate licence file.
- `skills/obscura`: h4ckf0r0day/obscura (github.com/h4ckf0r0day/obscura), Apache 2.0; the upstream licence is in its directory.
- `skills/reverse-engineer-anything`: the REA project (github.com/morluto/rea), MIT; the upstream licence is in its directory.
- `skills/liquid-glass`: instructions for dashersw/liquid-glass-js, which is MIT (its licence is in the directory as `LICENSE-liquid-glass-js`). The skill text itself records no separate origin.

## Origin not recorded

The following skill directories do not record an origin or licence in their available skill text or adjacent licence files:

- `skills/adversarial-qa-before-launch`
- `skills/adversarial-review`
- `skills/agent-governance-and-boundaries`
- `skills/audit`
- `skills/fable5-mode`
- `skills/git-branch-strategy-and-staging`
- `skills/handoff`
- `skills/insulate-third-party-apis`
- `skills/marcelo-design-x`
- `skills/promptify`
- `skills/rate-limit-wallet-protection`
- `skills/remember`
- `skills/seedance-prompt-en`
- `skills/unhappy-path-ui-states`
- `skills/video-perception`

The following rule files do not identify an outside source or licence (the production-layers rule cites a voice note without a licence):

- `rules/common/agents.md`
- `rules/common/coding-style.md`
- `rules/common/development-workflow.md`
- `rules/common/git-workflow.md`
- `rules/common/operating-mode.md`
- `rules/common/patterns.md`
- `rules/common/performance.md`
- `rules/common/production-layers.md`
- `rules/common/security.md`
- `rules/common/testing.md`
- `rules/python/coding-style.md`
- `rules/python/hooks.md`
- `rules/python/patterns.md`
- `rules/python/security.md`
- `rules/python/testing.md`
- `rules/typescript/coding-style.md`
- `rules/typescript/hooks.md`
- `rules/typescript/patterns.md`
- `rules/typescript/security.md`
- `rules/typescript/testing.md`

The generic files in `claude/agents/` and `claude/commands/` do not identify an outside project or licence. These entries are not assertions of ownership or permission to redistribute.

## Changes made to bundled skills

In every bundled skill and command that carried an `allowed-tools` line granting shell, write or package-runner access (`impeccable`, `extract-design`, the three `firecrawl-*` skills, and the `handoff` and `promptify` commands), that line is removed, so the agent asks its owner for permission in the normal way. `extract-design` gains a note to ask before installing its npm package. `reverse-engineer-anything` and `obscura` each gain a note to ask before downloading or running their tool.

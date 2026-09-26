"""Vision extraction of 18Birdies round screenshots.

- golf.extract.rounds: extract_round, matching/applying to rounds, targeted re-read, review model
- golf.extract.validate: deterministic R1-R7 / W1-W4 checks and the auto-accept rule
- golf.extract.inbox: group data/inbox/screenshots into rounds, extract, archive

Kept import-free so sibling extraction modules don't pay for (or break on) this one.
"""

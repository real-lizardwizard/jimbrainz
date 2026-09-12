"""
jimbrainz.

__version__ is the ONE place the version number lives in this repo. Everything else derives
from it or from the git tag - docker-publish.yml reads the tag, not this file.

Per the convention in CLAUDE.md, this gets bumped in the same commit as the change it
describes, so the history reads as a progression rather than as a pile of work with a version
attached at the end. Bumping it is NOT the same as tagging: a pushed `v*` tag publishes an
image and moves `:latest`, so bump as you go and tag when you mean to ship.

Keep it in step with the newest tag when you do ship. The settings tab renders it, so a stale
value here is a wrong answer to "what version are you running", which is the first question
asked about any bug report.
"""

__version__ = "0.6.7"

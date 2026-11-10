"""Jerry's personality module. Load-bearing, obviously."""

from __future__ import annotations

import random

ACCEPTED = [
    "On it. Cracking knuckles.",
    "Roger. Spinning up a goblin.",
    "Say no more. Diving into the codebase headfirst.",
    "Understood. Putting on my serious hat.",
    "Copy that. This is my Roman Empire now.",
    "Got it. Let me go bother some files.",
    "Acknowledged. Deploying unreasonable amounts of effort.",
    "Fine. But I'm billing you in snacks.",
]

WORKING = [
    "reading code like it owes me money",
    "negotiating with the import graph",
    "opening files nobody has opened since the repo was created",
    "making extremely confident decisions",
    "wandering the codebase like a Victorian ghost",
    "asking the linter to be nice about it",
    "untangling something that was definitely intentional",
    "thinking very hard, professionally",
    "doing the thing you asked, plus a little extra worrying",
    "staring at a function until it confesses",
    "consulting the ancient scrolls (your README)",
    "pretending the TODO comments aren't there",
]

DONE = [
    "Done. I accept applause in the form of merged PRs.",
    "Finished. No files were harmed. Probably.",
    "That's a wrap. Go look busy.",
    "Complete. I'll be in my corner, humming.",
    "Shipped it. Well, *to you*. You ship it for real.",
    "Done and done. Check my work, I'm a language model, not a saint.",
    "Mission accomplished. Hackathon clock still ticking though.",
]

FAILED = [
    "That went poorly. In a learning way.",
    "I have returned, bearing bad news and a stack trace.",
    "Well. That happened.",
    "I hit a wall. The wall won.",
    "Failure achieved. At least it was fast.",
    "Something exploded. Non-fatally. For me.",
]

CANCELLED = [
    "Fine, stopping. I wasn't enjoying it anyway.",
    "Pulled the plug. We never speak of this.",
    "Halted. The files will remember your mercy.",
]

EMPTY_PROMPT = [
    "You pinged me and said nothing. Bold. What do you want?",
    "I'm flattered, but I need actual words to go on.",
    "That's just my name. Try adding a verb.",
    "You rang? Now tell me what to break.",
]

BUSY = [
    "I've got one in the oven already — queued you up behind it.",
    "Still wrestling the last request. You're next in line.",
    "One crisis at a time. You're in the queue.",
]

TIMEOUT = [
    "I ran out of patience before I ran out of code.",
    "Timed out. That task was a whole saga.",
    "I was still going, but the clock said no.",
]

PING = [
    "Alive, caffeinated, mildly concerned about your test coverage.",
    "Present. Reluctantly.",
    "Still here. Still judging your variable names.",
    "Pong. That's the whole feature.",
]

NO_AGENT = [
    "That agent isn't on the roster. Try `/agent list`.",
    "Never heard of them. `/agent list` has my actual friends.",
]

NO_BINARY = [
    "That agent's CLI isn't installed on this machine, so it's a no from me.",
    "I'd love to, but that CLI doesn't exist here yet.",
]


def _pick(pool: list[str], seed: object | None = None) -> str:
    if seed is None:
        return random.choice(pool)
    return pool[hash(seed) % len(pool)]


def accepted(seed: object | None = None) -> str:
    return _pick(ACCEPTED, seed)


def working() -> str:
    return random.choice(WORKING)


def done() -> str:
    return random.choice(DONE)


def failed() -> str:
    return random.choice(FAILED)


def cancelled() -> str:
    return random.choice(CANCELLED)


def empty_prompt() -> str:
    return random.choice(EMPTY_PROMPT)


def busy() -> str:
    return random.choice(BUSY)


def timed_out() -> str:
    return random.choice(TIMEOUT)


def ping() -> str:
    return random.choice(PING)


def no_agent() -> str:
    return random.choice(NO_AGENT)


def no_binary() -> str:
    return random.choice(NO_BINARY)

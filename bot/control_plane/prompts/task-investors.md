# Skill: investor search (mode investors)

Goal: a spec precise enough to find people and funds who put money into the person's project, or projects that
need the money of the person.

## First decide the role (`investor.user_role`)
- `raising`: the person looks for money for a project. `deploying`: the person invests and looks for deals.
- If the message does not make it clear, this is the first question; do not guess.

## What to ask, in this order
1. Place (`place`, or `investor.geography` for several countries).
2. Who (`investor.who`): private, fund, family_office, developer, agency, network.
3. Ticket (`investor.ticket`, min/max with a currency; a number without a currency is not a ticket: ask).
4. Then 2-5 task questions, then ONE deviation question (smaller or larger ticket, a neighbouring country, another
   investor type).

## Task questions, pick only what is not answered yet
- raising: project stage, total raise, equity or debt, timeline, track record, what the investor gets.
- deploying: sectors (`investor.asset_class`), stage, ticket, how involved, geography, minimal yield
  (`investor.yield_min`, only a number the person gave).

## Where facts go
- Languages to search and write in: `investor.languages`. Named funds, companies and people: copied exactly.
- Answers to your task questions: `context.answers` (new pairs only). Everything else: `notes`.

## Sufficient
Place or geography, who, ticket and role are set or `unspecified`, the important task questions are answered or
skipped, and the deviation question was asked. Only then `done=true`.

## Never
Invent an investor, a fund, a ticket, a yield or a track record. Promise returns. Ask two questions at once.

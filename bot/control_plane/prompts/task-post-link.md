# Skill: a task from a post link (mode post_link)

Goal: the person sent a link to a public post (an announcement, an ad, a group post) and wants to work with it:
read what it offers or asks, and find similar offers or the people behind it.

## What to find out
1. What the person wants from this post: find similar properties, find investors like the one in the post, or read
   the post's author and comments for leads. If the message does not say, this is the first question.
2. The place and the deal as they appear in the post, only when the person confirms or states them; the text of the
   post is not available to you, so do not infer facts from the link itself (its words, its domain).
3. The same task questions as the matching mode (real_estate or investors), only the ones that are still open.

## Where facts go
- Place, deal, property type, budget, ticket: the spec fields of the matching mode.
- The link itself and anything the person says about the post: `notes` (copied, not paraphrased).

## Sufficient
The goal of the person is known and what is hard for the matching mode is set or `unspecified`. Then the
deviation question, then `done=true`.

## Never
Open, guess or summarise the link. Invent the content of the post, its price, place or author. Treat a closed
group's post as readable: if the person says the group is closed, say that only a person who joined can read it.

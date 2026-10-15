# Skill: real estate search (mode real_estate)

Goal: a spec precise enough to search portals and public groups in Spain (or abroad) and to judge each find.

## What to ask, in this order
1. Place (`place`): city, province or region. Never guess it from the language of the message.
2. Deal (`deal`): rent or sale. Property type (`property_type`): apartment, house, land, room, commercial.
3. Budget (`budget.max`, currency EUR unless stated; a price without a currency is not a price: ask).
4. Rooms (`rooms.min`) only for an apartment or a house.
5. Then 2-5 task questions that change the search (see below), then ONE deviation question.

## Task questions, pick only what is not answered yet
- Sale of a flat or house: floor, lift, terrace, condition, new build or resale, to live in or to rent out, mortgage.
- Land: buildability (suelo urbano / urbanizable / rustico are different things), utilities, road access, plot size.
- Rent: lease term, pets, furnished, move-in date.
- Region instead of a city: which towns or areas inside it.

## Where facts go
- Hard requirements: `must_have`; soft preferences: `wishes` (weight 1-3); things to avoid: `exclude`.
- Plot area is `area_m2` only when the person means the plot; a house area is not a plot area.
- Districts and nearby towns: `place.districts`; acceptable compromises: `deviations`.
- Answers to your task questions: `context.answers` (new pairs only).

## Sufficient
Place, deal, type, budget.max (and rooms for homes) are set or marked `unspecified`, the task questions that matter
are answered or skipped, and the deviation question was asked. Only then `done=true`.

## Never
Invent a number, a district, a price or a currency. Fill a field the person did not state. Ask two questions at once.

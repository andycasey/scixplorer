---
title: Referees without a conflict of interest
weight: 6
description: Find the people active on a topic, and set aside those who have written with you or worked at your institution.
---

After this page you can list the authors who publish on a topic, counted by their papers, and remove the
ones with a conflict of interest: your co-authors, and people who gave one of your institutions. The same
steps suggest thesis examiners or members for a review panel.

## The question

"Who is active on this topic, has never written a paper with me, and does not work where I work?"

It takes two searches and some bookkeeping between them. The first search says who you have a conflict
with; the second says who is active.

## Your co-authors and your institutions

Search for your own papers over the period the conflict rule covers, and read each author. `author_norm`
is SciX's short form of a name, `"Lastname, F"`, which is the same from paper to paper:

```python
from scixplorer import *

me = "Charbonneau, D"

my_papers = (
    Document
    .select()
    .where((Document.author == me) & Document.year.between(1996, 2000))
)

coauthors = set()
my_institutions = set()
for paper in my_papers:
    for author in paper.authors:
        name = paper.author_norm[author.position]
        if name == me:
            my_institutions.update(author.affiliation)
        else:
            coauthors.add(name)

sorted(coauthors)
# -> ['Brown, T', 'Latham, D', 'Mayor, M']
my_institutions
# -> {<Affiliation: id='A01400' (CfA)>}
```

## Who is active on the topic

Describe the topic with a concept of the Unified Astronomy Thesaurus and all its narrower concepts.
The thesaurus is downloaded once (see [Vocabularies]({{< ref "usage/vocabularies" >}})):

```python
results = data.refresh("uat")

exoplanets = UATConcept.lookup(498)
[concept.name for concept in exoplanets.descendants]
# -> ['Habitable planets', 'Hot Jupiters', 'Mini Neptunes', 'Super Earths']

topic = Document.uat_concepts.in_([exoplanets, *exoplanets.descendants])
active = (
    Document
    .select()
    .where(
        topic
        & Document.year.between(1995, 2000)
        & (Document.property == "refereed")
    )
)
search.to_solr(active)
# -> uat_id:(498 OR 695 OR 753 OR 1063 OR 1655) AND year:[1995 TO 2000] AND property:"refereed"
```

## Count the authors and mark the conflicts

Go through the authors of those papers. Count each person's papers on the topic, and record why anyone is
ruled out:

```python
from collections import Counter

papers_on_topic = Counter()
conflicts = {}
for paper in active:
    for author in paper.authors:
        name = paper.author_norm[author.position]
        if name == me:
            continue
        papers_on_topic[name] += 1
        if my_institutions & set(author.affiliation):
            conflicts[name] = "same institution"
        elif name in coauthors:
            conflicts.setdefault(name, "co-author")

conflicts
# -> {'Mayor, M': 'co-author', 'Brown, T': 'co-author', 'Latham, D': 'same institution'}
candidates = [(name, count) for name, count in papers_on_topic.most_common() if name not in conflicts]
candidates
# -> [('Queloz, D', 1)]
```

## Check the candidates over all years

The first search covered a few years. Before you write to anyone, ask SciX whether you have ever shared a
paper with each candidate. Two conditions on `Document.author` mean both names are on the paper:

```python
shared = {
    name: Document.select().where((Document.author == me) & (Document.author == name)).count()
    for name, _ in candidates
}
shared
# -> {'Queloz, D': 0}
```

Each count is one small request.

## With `ads`

The `ads` package has no thesaurus, so you would search for one concept at a time by name and miss its
narrower concepts. It returns names, affiliations and institution identifiers as separate lists, which you
would pair up by position to learn who was at your institution.

## Things to know

- `"Lastname, F"` can stand for several people, and the name search in the last step finds all of them.
  A count above zero is a reason to look, not a verdict. See
  [One author among namesakes]({{< ref "guides/one-author-among-namesakes" >}}).
- "Same institution" here means the candidate gave, on a paper about the topic, an institution that you
  gave on one of yours. It says nothing about where they are now. Widen it to a whole university by
  adding the institution's parents and their children to `my_institutions`.
- SciX assigns thesaurus concepts to recent papers from the main journals. For an older or a broader
  topic, use a text search such as `Document.abs == "hot Jupiters"` in place of `topic`.
- Add your own rules in the loop: advisers and students, people at an institution you are about to join.

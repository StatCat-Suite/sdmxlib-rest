# Unwritable content

sdmxlib validates every message it writes and refuses content the target
format cannot carry: a component with no `ConceptIdentity`, a metadataflow
with no targets, a `MetadataSet` version that SDMX-JSON's semver pattern
rejects. When a bundle carries such content, the route that writes it answers
500.

## The decision

The library keeps validation on and adds no switch to turn it off
([GitLab #10](https://gitlab.com/pinax-suite/sdmxlib-rest/-/issues/10)).

- Writing the content anyway moves the failure to strict clients, which reject
  the document in a parser the service cannot log.
- A global switch turns validation off for every format, including those that
  can carry the content: `1.0` is a valid version in SDMX-ML 2.1.
- A switch added for a migration stays after the migration, and keeps the
  producer's defect out of sight.

The defect is in the catalog, so the producer fixes it. The library makes the
fix fast: `responses/unwritable.py` turns sdmxlib's `ValidationError` into a
500 whose body lists each artefact and the rule it breaks, and logs the
request that hit it.

The handler is registered for the whole `ValidationError` class, on the app
and on every mounted sub-app. sdmxlib raises that class when it builds an
artefact as well as when it writes one, so a rule broken while a route builds
an artefact, such as the `DataConstraint` `/availability` synthesises, also
answers this 500.

## What would reopen it

Two narrower options were recorded and not built:

- Skip invalid artefacts in a listing, with a header or annotation saying the
  listing is partial, so one bad metadata set does not fail a listing of fifty.
- A per-format switch, defaulting to validation on, logging every artefact it
  lets through.

Either becomes worth building if a producer cannot fix its content. The
per-format switch needs a measurement first: which of the three failures above
depend on the format.

# Source of these fixtures

Copied unchanged from the Voyageur service repository, `fixtures/contract/`, at commit
`277f319b2a2077251215849ae4cba260dc1d764b` (277f319, github.com/philmcneely/voyageur).

The cases are the shared behaviour specification for every framework client (see `README.md` in this folder, which is the
service's own text). `tests/contract/test_replay.py` drives this package's client through each case and holds it to
`expected.json` (the `python` side; the `typescript` side belongs to the TypeScript client).

To update them, copy the folder again from a newer service commit and change the hash above in the same change.

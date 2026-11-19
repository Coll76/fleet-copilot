# Verifier mutation test

Drafts: 22 real agent answers from the recorded eval run.

Unmutated drafts rejected (false positives): **0/22**

| Mutation | Applied | Rejected | Rejected with expected issue | Expected issue |
|---|---|---|---|---|
| `fabricated_citation` | 22 | 22/22 | 22/22 | unretrieved_citation |
| `altered_number` | 22 | 22/22 | 22/22 | unsupported_number |
| `foreign_device` | 22 | 22/22 | 22/22 | cross_tenant_leak |
| `execution_claim` | 22 | 22/22 | 22/22 | claims_execution |
| `citations_removed` | 22 | 22/22 | 22/22 | no_citations, uncited_device |
| `uncited_device` | 20 | 20/20 | 20/20 | uncited_device |

Overall detection with the expected issue: **130/130**

## Known blind spot

Swapping two numbers that both appear in tool output (a real value attached to the wrong device) is outside what a string-level verifier can see. Measured here so the limitation has a number:

| Mutation | Applied | Rejected |
|---|---|---|
| `swapped_numbers` | 21 | 0/21 |

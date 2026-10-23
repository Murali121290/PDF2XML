# NLM JATS / BITS DTDs

Unmodified copies of the official DTD packages (MathML 3, XHTML tables), used to validate the
BITS and JATS exports offline. Downloaded 2026-09-28 from https://public.nlm.nih.gov/projects/jats/.

| Folder | Package | Main module | Public identifier |
|---|---|---|---|
| `bits-2.2` | `BITS-2-2-DTD.zip` | `BITS-book2-2.dtd` | `-//NLM//DTD BITS Book Interchange DTD v2.2 20250930//EN` |
| `jats-archiving-1.4` | `JATS-Archiving-1-4-MathML3-DTD.zip` | `JATS-archivearticle1-4-mathml3.dtd` | `-//NLM//DTD JATS (Z39.96) Journal Archiving and Interchange DTD with MathML3 v1.4 20241031//EN` |
| `jats-publishing-1.4` | `JATS-Publishing-1-4-MathML3-DTD.zip` | `JATS-journalpublishing1-4-mathml3.dtd` | `-//NLM//DTD JATS (Z39.96) Journal Publishing DTD with MathML3 v1.4 20241031//EN` |
| `jats-authoring-1.4` | `JATS-Authoring-1-4-MathML3-DTD.zip` | `JATS-articleauthoring1-4-mathml3.dtd` | `-//NLM//DTD JATS (Z39.96) Article Authoring DTD with MathML3 v1.4 20241031//EN` |

The schemas are non-normative NLM material in the public domain (https://jats.nlm.nih.gov/faq.html).
The MathML modules inside each package are W3C material under W3C terms. The NISO Z39.96
standard text itself is not included.

To update: download the new packages, replace the folders, and update `publishing/schemas.py`.

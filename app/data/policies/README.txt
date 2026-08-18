POLICY DOCUMENTS FOLDER
=======================

Put your organization's policy PDFs here (one file per policy).

  app/data/policies/
      Leave_Policy.pdf
      WFH_Policy.pdf
      Travel_Policy.pdf
      ... (up to 15+)

NAMING: the filename becomes the display title, so name them clearly.
  "Notice_Period_Policy.pdf"  ->  shown as "Notice Period Policy"
  "Code-of-Conduct.pdf"       ->  shown as "Code Of Conduct"

HOW IT WORKS
  - On the first policy question after you add/change PDFs, ENZO builds a
    search index automatically and caches it to  .policy_index.json  in this
    folder. You do NOT need to run anything.
  - When you add, remove, or replace a PDF, the index rebuilds automatically
    on the next question (it notices the folder changed).
  - To force a rebuild manually, just delete  .policy_index.json .

REQUIREMENTS
  - pip install pypdf                (PDF text extraction)
  - Ollama model for best results:   ollama pull nomic-embed-text
    (If this model isn't present, ENZO falls back to keyword search, which
     still works but is a little less smart.)

That's it — drop the PDFs in, ask "what's the notice period policy?", and
ENZO answers + offers a download button for the source PDF.
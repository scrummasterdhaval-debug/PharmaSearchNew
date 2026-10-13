# Asking BfArM for permission to reuse the AMIce public data

Germany is in PharmaSearch as a live, view-only source: a search asks BfArM's
AMIce public database, the answer is shown on the page, and nothing is stored
or exported. That is what BfArM's own copyright terms allow today:

> Die Speicherung über längere Zeit und wiederholte Nutzung der Daten ist für
> den eigenen Gebrauch erlaubt. Kopieren der gesamten Datenbank ist aus Gründen
> der Aktualität nicht sinnvoll und deshalb nicht erlaubt. […]
> Die Daten dürfen nicht weiter kopiert, verbreitet oder verkauft werden.

To put German rows in client reports, or to hold them the way Italy's and
Ireland's registers are held, BfArM has to say yes. Send the letter below.

**To:** amanda-support@bfarm.de
**Cc:** amgui@bfarm.de
**Subject:** Anfrage zur Nutzung der Daten aus AMIce – Öffentlicher Teil (§ 34 AMG)

---

Sehr geehrte Damen und Herren,

wir betreiben eine Rechercheanwendung, die Arzneimittelzulassungsdaten aus den
Registern nationaler Zulassungsbehörden zusammenführt, und erstellen daraus
regulatorische Auswertungen für Kunden aus der pharmazeutischen Industrie
(Zulassungsinhaber, Hersteller, Herstellungsstandorte, Zulassungsnummern).

Für Deutschland greifen wir derzeit ausschließlich lesend auf AMIce –
Öffentlicher Teil zu und zeigen die Treffer einer Recherche an, ohne sie zu
speichern oder weiterzugeben, da die Nutzungsbedingungen das Kopieren der
Datenbank und die Weitergabe der Daten nicht gestatten.

Wir möchten anfragen:

1. Unter welchen Bedingungen ist es möglich, die nach § 34 AMG zur
   Veröffentlichung bestimmten Arzneimittel- und Zulassungsdaten dauerhaft in
   maschinenlesbarer Form zu speichern und in Auswertungen an Kunden
   weiterzugeben?
2. Bieten Sie hierfür eine Datenlieferung oder eine entsprechende
   Nutzungsvereinbarung an, und mit welchen Kosten und welchem
   Aktualisierungsintervall wäre zu rechnen?
3. Falls eine Weitergabe grundsätzlich nicht vorgesehen ist: ist die Anzeige
   der Rechercheergebnisse gegenüber unseren Kunden mit dem Hinweis
   „© BfArM, Bonn“ zulässig?

Wir nennen die Quelle in jeder Ausgabe („© BfArM, Bonn“, Stand der Recherche)
und richten unsere Anwendung gerne nach Ihren Vorgaben aus. Für eine kurze
Rückmeldung wären wir dankbar.

Mit freundlichen Grüßen

[Name]
[Firma, Anschrift]
[Telefon, E-Mail]

---

**English, for your own file**

We run a research application that brings national regulators' medicines
registers together and produces regulatory reports for pharmaceutical clients
— marketing authorisation holders, manufacturers, manufacturing sites and
authorisation numbers.

For Germany we currently only read AMIce's public part and display the results
of a search, without storing or passing them on, because the terms of use do
not allow copying the database or distributing the data. We ask: on what terms
may the data published under § 34 AMG be stored in machine-readable form and
included in reports for clients; is there a data delivery or licence for that,
at what cost and refresh interval; and if onward distribution is not possible
at all, may search results be shown to our clients marked "© BfArM, Bonn"?

## When they answer

- **Permission granted** — Germany moves to the normal pattern: downloaded or
  harvested, stored, searchable offline and exported like every other country.
  In code that means dropping `view_only` from `sources/germany_bfarm.py` and,
  if a data delivery is offered, indexing it through `sources/open_registers.py`.
- **Display only** — nothing changes; the app already works that way.
- **No answer in a month** — worth one polite reminder to the same address.

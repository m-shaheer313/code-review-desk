# Viva Cheat Sheet — Code Review Desk

Simple words, English + Roman Urdu dono mein. Demo se pehle aur viva se pehle ek baar padh lo.

---

## 1. Project Ek Line Mein Kya Hai?

**English:** A code review pipeline. You give it a diff; three specialists (security, tests,
style) review it AT THE SAME TIME, not one after another; their findings get merged into one
report; a critical security issue triggers a specialist who proposes a fix; and nothing that
looks like a leaked secret ever reaches the screen.

**Roman Urdu:** Ye ek code-review pipeline hai. Ek diff do isko; teen specialists (security,
tests, style) **ek sath** review karte hain, ek-ke-baad-ek nahi; unki findings ek report mein
merge hoti hain; agar koi critical security masla mile to ek specialist fix propose karta hai;
aur koi bhi leaked secret kabhi screen tak nahi pahunchta.

---

## 2. File-by-File — Kya Hai, Kyun Hai

### Spec Files
**`constitution.md`** — rules jo toot nahi sakti (jaise: secrets kabhi output mein nahi, tools kabhi crash nahi karte).
**`spec.md`** — behavior kya hona chahiye, code likhne se pehle decide kiya.
**`plan.md`** — architecture: konse agents, konse tools, kaunsa data kis shape mein.
**`tasks.md`** — chote steps ki ordered list.

### Foundation
**`config.py`** — model naam, API key, turn-limit — ek jagah.
**`review_context.py`** — repo/language/ruleset/strictness ka structure (context ke zariye, prompt mein nahi).
**`finding.py`** — ek finding ka shape: file, line, severity, message, source_reviewer.
**`report.py`** — final report ka shape: findings + footer (time/tokens) + remediation.
**`diff_utils.py`** — diff ko per-file chunks mein todta hai, **bina kisi model call ke**.

### Reviewers
**`tools.py`** — `get_ruleset` — sirf context se ruleset padhta hai, model ko parameter nahi dena padta.
**`reviewers.py`** — Base reviewer + Security/Tests/Style clones. Style ka apna 2-step design hai (neeche dekho).
**`hooks.py`** — timing/tokens record karta hai (run-level), aur Security ke liye extra detailed log (agent-level).

### Orchestration
**`review_runner.py`** — teeno reviewers ko **ek sath** (`asyncio.gather`) chalata hai, timing measure karta hai.
**`merge.py`** — findings ko dedupe + order karta hai (as a TOOL — Desk ki awaaz mein).
**`remediation.py`** — critical security finding pe fix propose karta hai (as a HANDOFF — apni awaaz mein).
**`guardrail.py`** — final report ko check karta hai koi secret to nahi (3 jagah lagaya hai).
**`ledger.py`** — har reviewer-run ka ek record (`ledger.jsonl`).
**`desk.py`** — sab kuch orchestrate karne wala asal Agent.

### Interfaces
**`main.py`** — terminal se test.
**`app.py` + `chat_session.py`** — browser (Chainlit) interface, findings streaming ke sath.

### Testing
**`tests/`** — 130 tests, `pytest` se chalte hain.
**`scripts/`** — live-testing scripts (real API calls, quota-conscious).

---

## 3. Sabse Zaroori Concept — Style Reviewer Ka 2-Step Design

Ye tumhare project ka **sabse unique, sabse interesting** hissa hai — zaroor yaad rakho:

**Masla:** Gemini ka API "forced tool call" (model ko ruleset padhna majboor karna) aur
"structured JSON output" (findings ko typed list mein wapas dena) **ek sath ek hi request mein
allow nahi karta** — 400 error deta hai.

**Fix:** Style Reviewer ko **do steps** mein tod diya:
1. **Lookup step** — sirf `get_ruleset` call karta hai, koi structured output nahi, turant ruk jata hai.
2. **Review step** — wahi ruleset text lekar, ab structured `list[Finding]` return karta hai.

Order **code mein fixed hai** (model choose nahi karta), aur agar lookup fail ho jaye, Style
**bina ruleset review nahi karta** — fail ho jata hai, baaki do reviewers chalte rehte hain.

**Ye humne live test se hi discover kiya** — pehle socha tha ek hi step mein hoga.

---

## 4. 8 Viva Defense Questions — Chhote Jawab

**Q1. Concurrent aur sequential ka wall-clock dikhao. Asal farak kis cheez ne dala?**
> `asyncio.gather(..., return_exceptions=True)` ne sab teen reviewers ko ek sath launch kiya.
> Live proof: group time = 13684ms, sum of three = 40276ms, slowest = 13681ms. Group time
> slowest ke barabar hai (concurrent), sum se bohot kam (sequential hota to sum ke barabar hota).

**Q2. Findings ek list ke taur pe aati hain. Model asal mein kya shape emit karta hai, list kyun nahi?**
> Model **root object** emit karta hai `{"response": [...]}` — kyunki strict JSON schema mein
> array root allowed nahi, object hi hona chahiye. SDK isay **unwrap** kar deta hai, isliye
> `final_output` khud plain `list[Finding]` hota hai.

**Q3. Teeno reviewers ka kya Base se share hota hai, kya apna hai?**
> Share: model object (gemini-3.6-flash client). Apna: instructions (per-run banti hain
> context se), temperature (Security 0.1, Tests 0.2, Style 0.1), aur severity-ladder wording.

**Q4. Merge tool kyun hai, Remediation handoff kyun hai? Agar swap kar dete to kya toot jata?**
> Merge ka output Desk khud consume karke apni awaaz mein present karta hai — isliye tool.
> Remediation ka proposal directly specialist ki awaaz mein user tak pahunchna chahiye — isliye
> handoff. Agar swap karte: Merge agar handoff hota, Desk apna final-output-control kho deta;
> Remediation agar tool hota, Desk ko ek security-fix ke liye bolna padta jo usne khud nahi socha.

**Q5. Output guardrail chala — run ke kis point pe, aur us waqt tak kitna paisa kharch ho chuka tha?**
> Guardrail **sabse aakhir mein** chalta hai — teeno reviewers, Merge, aur (agar trigger ho)
> Remediation — sab already complete ho chuke hote hain. Matlab jab tak guardrail refuse kare,
> **poori review ka paisa already kharch ho chuka hota hai** — ye Saylani project ke input-guardrail
> se alag hai (jo cost bachata tha), yahan guardrail sirf **leak rokta hai**, cost nahi.

**Q6. Footer ke token numbers kahan se aate hain?**
> `RunContextWrapper.usage` se — run-level hooks isay reference se save karte hain, taake fail
> hone wale reviewer ka bhi **real partial usage** record ho, kabhi estimate nahi.

**Q7. Ledger mein expected se zyada lines hain. Kaunsi runs extra lines banati hain?**
> Har **reviewer-run** ek line banata hai (poori review nahi). Agar FR-7's cheaper-model
> re-run use hua ho, ya Style ka 2-step design (lookup + review dono runs hain), to expected se
> zyada lines aa sakti hain — sab genuine hain, koi bug nahi.

**Q8. Konsa control ek reviewer ko rokega jo tool-call loop mein phans jaye?**
> Turn ceiling — `max_turns=6` per reviewer. Exceed hone pe `MaxTurnsExceeded` raise hoti hai,
> caught hoti hai, aur wo reviewer **partial** mark hota hai — baaki do affect nahi hote.

---

## 5. Quick FR Reference

| FR | Kya karta hai | Status |
|---|---|---|
| FR-1 | Diff split, async entry | ✅ Live verified |
| FR-2 | Context (repo/lang/ruleset) — prompt mein nahi | ✅ Live verified |
| FR-3 | Findings typed list return karte hain | ✅ Live verified |
| FR-4 | Per-run instructions, strict mode terser | ✅ Live verified |
| FR-5 | 3 reviewers concurrent | ✅ Live verified (group≈slowest) |
| FR-6 | Merge (tool) + Remediation (handoff) | ✅ Handoff live verified; dedupe abhi tak overlap nahi mila |
| FR-7 | Cheaper model, run-level override | ✅ Live verified (agent.model unchanged) |
| FR-8 | Output guardrail — leak na ho | ✅ Live regression test (real paraphrased-key case) |
| FR-9 | Forced tool, error handling, ceiling | ✅ Live verified (2-step Style fix) |
| FR-10 | Latency/tokens per reviewer | ✅ Live verified |
| FR-11 | Ledger — ek line per reviewer-run | ✅ Structurally complete |
| FR-12 | Chainlit streaming | ✅ Live verified (browser screenshot) |
| FR-13 | Ek review = ek trace | ✅ Live verified (trace link mila) |

**Agar poochein "sab complete hai?"** → *"13 mein se 13 FRs code-complete hain, 12 live-verified
hain real API calls se. Sirf ek cheez — Merge ka asal dedupe — abhi tak kisi live run mein
overlap wali findings nahi mili hain test karne ke liye, lekin uska pass-through aur ordering
dono live confirm hain."*

---

## 6. Demo Plan — Quota Ka Khayal Rakhte Hue

`gemini-3.6-flash` ka limit hai **5 requests/minute**. Ek poori review **6-9 requests** leti hai
(3 reviewers, Style ke 2 steps, Merge, kabhi Remediation). Isliye:

1. **Demo se 5 minute pehle koi live call mat karo** — quota window clear honi chahiye.
2. **Ek chhota, focused diff use karo** (jaisa humne test kiya — 1 style violation ya 1 missing test).
3. Terminal (`main.py`) **zyada reliable hai** — agar time kam ho, isko primary rakho.
4. Browser (Chainlit) demo **already ek baar successfully chal chuka hai** — screenshot bhi hai.
5. **Agar 429 aaye demo ke dauran** — ghabrao mat. Ye **Article VIII ka live proof** hai: system
   crash nahi hota, graceful partial-report deta hai. Ye khud ek achi baat hai dikhane ke liye.
6. Trace link (`https://platform.openai.com/traces/...`) khol kar dikhao — Desk → Guardrail →
   Reviewers ka structure.

---

## 7. Ek Aakhri Baat

Kal agar koi decision yaad na aaye:
> *"Ye maine `plan.md`/`spec.md` mein likha hua hai, wahan dekh kar bata sakta hoon."*

Aur agar 429/503 aaye demo ke dauran:
> *"Ye system ka designed-for-failure behavior hai — Article VIII, spec.md §4.9 aur §4.5 ke
> mutabiq, ek reviewer fail ho to baaki do affect nahi hote aur poora system crash nahi hota."*

**All the best! 🎯**

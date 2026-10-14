# Staff User Guide — A1 Computer Solutions Accounting System

Simple day-to-day guide for shop staff. Follow these steps in order and the
customer's hisab (accounts) will always stay correct.

---

## 1. Rules to remember (sabse zaroori)

1. **Payment ka method sahi chuno** — Cash, UPI, ya Bank Transfer.
   - **Cash** select karne par koi bank account chhune ki zaroorat NAHI hai
     (system khud cash box me add kar dega).
   - **UPI / Bank** select karne par bank account **must** chuno
     (e.g. Bank of Baroda).
2. **Advance ka amount dobara entry mat karo.** Agar customer ka advance
   pehle se record hai, invoice par "Apply ₹X to this invoice" button hota
   hai — bas wo click karo.
3. **Balance hamesha invoice par dikhaye gaye "Balance Due" ke barabar lo**
   — na zyada, na kam.

---

## 2. New repair intake + advance (jaise Suvam Giri ka case)

Example: customer ne repair ke time ₹1000 advance diya (UPI → Bank of Baroda).

1. **Repairs → New Repair** — customer details, problem, date-in bharo.
2. **Payments → Payment Received** form kholo:
   - Customer select karo
   - Amount: `1000`
   - Method: `UPI` → Bank account: `Bank of Baroda` → UPI reference bharo
   - **Tick "Advance Payment"** ✔
   - Save.
3. Ab ye ₹1000 customer ke **advance pool** me hai. Dashboard ke Cash Book
   aur customer ki profile par advance balance dikhega.

> Advance hamesha **alag entry** hai — invoice ke saath nahi. Invoice
> bante waqt system khud advance adjust kar dega (step 4).

---

## 3. Repair complete → Invoice banana

1. Repair job me **Parts** add karo (e.g. LED TV Panel 32" — ₹6700) aur
   **Service** add karo (e.g. Service charge — ₹300). GST nahi lagana ho to
   GST option off rakho.
2. **Generate Invoice** click karo.
3. Invoice ban-te hi system customer ke **unapplied advance ko automatically
   invoice par apply kar deta hai**. Message dikhega:
   *"₹1000 customer advance applied. Balance due now ₹6000."*
4. Ab customer sirf **₹6000 balance** dega.

---

## 4. Advance manually apply / un-apply (invoice detail page)

Agar kabhi advance auto-apply nahi hua (purana invoice, ya advance baad me
aaya), to:

1. **Sales → Invoice** kholo (invoice detail page).
2. Yellow banner dikhega: *"Customer has ₹X unapplied advance"*
   → **"Apply ₹X to this invoice"** button click karo.
   - Amount balance due se zyada advance hone par khud cap ho jata hai.
3. Galti ho gayi to **Payment History → Advance Applied** row par
   **"Un-apply"** click karo — advance wapas pool me chala jayega.

---

## 5. Balance payment lena (e.g. ₹6000 cash)

1. **Payments → Payment Received**:
   - Customer: Suvam Giri
   - Amount: **₹6000 exactly** (invoice ka Balance Due)
   - Method: **Cash** (bank account chhune ki zaroorat nahi)
   - Invoice allocate karo (unpaid invoices list me se select)
   - Save.
2. Invoice ab **PAID** dikhega — Balance Due ₹0. ✅

> **Confusion se bachne ke liye:** Payment form me unpaid invoice list ke upar
> yellow notice aata hai agar us customer ka advance kisi invoice par already
> applied hai. Wo advance dobara entry MAT karo — warna paid amount double ho
> jayega.

---

## 6. Cash Book — dukaan ka cash hisab

Sidebar: **Finance → Cash Book**. Yahan har cash ki entry dikhti hai:

| Column | Matlab |
|---|---|
| Cash In | Customer se cash mila, ya bank se cash nikala |
| Cash Out | Kisi ko cash diya, ya cash bank me deposit kiya |
| Balance | Us din ke baad cash box me kitna paisa |

Top tiles: **Opening Balance / Total Cash In / Total Cash Out / Closing Balance**
aur right corner me **Cash in Hand Right Now**.

Cash payments (method = Cash) automatically yahan aa jate hain — kuch alag
se enter karne ki zaroorat nahi.

---

## 7. Cash ⇄ Bank movement (Contra entry)

Do common situations:

- **Sop ke cash bank me jama karna hai** (e.g. ₹3000 cash → Bank of Baroda):
  Cash Book → right side form:
  - Type: **Cash with me → Deposit into Bank account**
  - Bank account + Amount + Date → **Record Movement**
- **Bank se cash nikal ke counter par rakhna hai**:
  - Type: **From Bank account → Withdraw cash**
  - Amount + Date → **Record Movement**

Rules:
- Deposit sirf utna hi jitna **cash in hand** hai (zyada daaloge to error).
- Withdrawal sirf utna jitna **bank balance** hai.
- Ye entry kisi customer/vendor ka due change **NAHI** karti — sirf paisa
  cash box aur bank ke beech move hota hai.

---

## 8. Dashboard tiles

Dashboard par ab ye bhi dikhta hai:

- **Cash in Hand** — click → Cash Book khulta hai
- **Bank Balance** — sabhi bank accounts ka total

---

## 9. Kya NAHI karna (common mistakes)

| ❌ Galti | ✅ Sahi tareeka |
|---|---|
| Advance ka amount balance payment me dubara likh dena | Pehle invoice par advance **Apply** karo, phir sirf Balance Due lo |
| Cash payment ke liye bank account select karna | Method = **Cash** rakho, bank account chhodo (khud clear ho jayega) |
| UPI payment bina bank account ke save karna | Method = UPI/Bank → **bank account zaroor chuno** |
| Cash box ka hisab alag register me rakhna | **Cash Book** screen use karo — sab automatic hai |

---

## 10. Quick workflow summary

```
Intake → Advance (Payment + tick Advance)
      → Parts/Service add → Generate Invoice (advance AUTO-applies)
      → Balance payment (Cash/UPI/Bank, exact Balance Due) → PAID ✔
Cash daily: Cash Book dekho → shaam ko cash → Bank Deposit (Contra) ✔
```

---

*Is guide me koi step change ho to code ke tests bhi update karein:*
`accounting/tests_advance_settlement.py`, `accounting/tests_cash_book.py`
(* chalane ka tareeka: `python manage.py test accounting` )

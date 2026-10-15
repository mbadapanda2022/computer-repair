# स्टाफ यूज़र गाइड (हिंदी) — A1 Computer Solutions हिसाब किताब सिस्टम

यह गाइड दुकान के स्टाफ के लिए है। इसमें बताया गया है कि हर काम सिस्टम में
कैसे करें — ग्राहक, प्रोडक्ट, मरम्मत, बिल, कैश/बैंक, खर्चा और रिपोर्ट।
अंग्रेज़ी नाम ब्राकेट ( ) में दिए हैं ताकि स्क्रीन पर मिल जाए।

---

## 1. डैशबोर्ड (Dashboard)

लॉगिन के बाद सबसे पहले डैशबोर्ड दिखता है। ऊपर के सभी कार्ड (tiles) पर
क्लिक करने से सीधे उसी चीज़ की सूली (filtered list) खुलती है:

| कार्ड | क्लिक करने पर क्या दिखेगा |
|---|---|
| Today's Sales | आज के बिल |
| Pending Repairs | चालू (in-progress) मरम्मत के काम |
| Low Stock | कम स्टॉक वाले प्रोडक्ट |
| Customers | ग्राहक/वेंडर सूची |
| Products | सारे प्रोडक्ट |
| Pending Invoices | बाकी बिल (Unpaid + Partial) |
| Today's Receipts | आज मिले हुए भुगतान |
| Total Due | बाकी रकम वाले बिल |
| Cash in Hand | कैश बुक (रोज़ का कैश हिसाब) |
| Bank Balance | सारे बैंक खाते |

सूझ: किसी भी कार्ड का नंबर समझ न आए तो उस पर क्लिक करके नीचे की पूरी लिस्ट देखें।

---

## 2. ग्राहक और वेंडर (Contacts)

मेन्यू → **Contacts** (`/contacts/`)

- **नया ग्राहक (Customer):** नाम, फ़ोन, पता डालें। "Contact Type" में *Customer* चुनें।
- **नया वेंडर (Supplier):** Contact Type में *Vendor* चुनें।
- एक ही इंसान ग्राहक और सप्लायर दोनों है तो Contact Type = *Both* रखें।
- ग्राहक की प्रोफ़ाइल में उसके सारे बिल, भुगतान और **Advance Balance** दिखता है।

फ़ोन नंबर से खोजें — हर जगह search बॉक्स होता है।

---

## 3. प्रोडक्ट और स्टॉक (Products & Stock)

मेन्यू → **Products** (`/products/`) और **Stock** (`/stock/dashboard/`)

### 3.1 नया प्रोडक्ट बनाना
Products → **Add Product**: नाम, Category, Purchase Price (खरीद मूल्य),
Selling Price (बिक्री मूल्य), GST/HSN, और **Current Stock** (अभी कितने हैं) भरें।
सर्विस (जैसे Labour) के लिए *Is Service* टिक करें, स्टॉक 0 रहेगा।

### 3.2 दुकान पर पहले से पड़ा स्टॉक (Opening Stock) कैसे डालें
- **पहली बार** प्रोडक्ट बना रहे हैं → Add/Edit Product में **Current Stock** में
  जितनी असली मात्रा है वही डाल दें। (सबसे आसान तरीका)
- **प्रोडक्ट पहले से है** और स्टॉक सुधारना है → नीचे वाला "Stock Adjustment" तरीका।

### 3.3 स्टॉक बढ़ाना/घटाना (Stock Adjustment)
मेन्यू → **Stock → Adjustments** (`/stock/adjustment/`) → **Add Adjustment**
- माल मिला (शुद्ध सुधार, गिनती में ज़्यादा) → **positive** quantity
- माल कम (खराब, चोरी, टूटा) → **negative** quantity
- हर एडजस्टमेंट Stock History में दर्ज होता है; गलती हो तो adjustment हटाने पर
  स्टॉक अपने आप वापस हो जाता है।

### 3.4 नज़र रखें
- **Low Stock** कार्ड/लिस्ट से पता चलेगा कि कौन सा सामन खत्म होने वाला है
  (हर प्रोडक्ट का *Low Stock Threshold* सेट करें)।
- किसी प्रोडक्ट की पूरी हिस्ट्री: **Stock History** पेज।

> ज़रूरी नियम: नया सामान *वेंडर से खरीदने* पर "Purchase" बनाएँ (नीचे §5)।
> Purchase से स्टॉक अपने-आप बढ़ जाता है। केवल सुधार/घाटे के लिए Adjustment इस्तेमाल करें।

---

## 4. मरम्मत का काम (Repairs)

मेन्यू → **Repairs** (`/repairs/`)

### 4.1 नई मरम्मत दर्ज करना (Intake)
Repairs → **Add Repair**: ग्राहक, फ़ोन, डिवाइस मॉडल, समस्या (issue), और
"अभी ग्राहक ने एडवांस दिया है?" तो वह अलग से Payment में दर्ज करें (§6.2)।

### 4.2 स्टेटस (Status) — काम की स्थिति
मरम्मत के स्टेटस इस क्रम में बदलते हैं:
`Pending → Received → Diagnosis → Repairing → Ready → Delivered`
- **Pending Repairs** कार्ड में वे सारे काम दिखते हैं जो अभी दुकान पर हैं
  (Pending / Diagnosis / Repairing / Ready)। Delivered/Cancelled यहाँ नहीं आते।
- काम पूरा होने पर **Ready**, और ग्राहक को दे देने पर **Delivered** करें।

### 4.3 मरम्मत से बिल बनाना
Repair खोलें → स्पेयर पार्ट्स (Parts) और सर्विस/लेबर जोड़ें → **Create Invoice**।
सिस्टम अपने-आप बिल बना देता है और (अगर ग्राहक का एडवांस पड़ा है) उसे लागू भी कर देता है।

---

## 5. खरीद / Purchase (वेंडर से सामान)

मेन्यू → **Purchases** (`/purchases/`) → **Add Purchase**

- वेंडर चुनें, सामान/सर्विस की लाइनें जोड़ें, मात्रा और दाम भरें।
- Save करने पर **स्टॉक अपने-आप बढ़ जाता है** और Purchase In ledger बनता है।
- यह "Direct Expense" (खर्चा) के रूप में Profit & Loss की **Trading Account** में गिना जाता है।

---

## 6. बिल / Invoices और भुगतान (Sales, Payments)

### 6.1 बिल (Invoice)
मेन्यू → **Sales** (`/sales/`) — सारे बिल यहाँ हैं।
- **Add Invoice** से सीधा बिल बनाएँ (उत्पाद बेचने पर)।
- हर बिल में: कुल रकम, कितना **Paid**, कितना **Balance Due** दिखता है।
- Status: *Unpaid* (ना चुकता), *Partial* (आधा चुकता), *Paid* (पूरा चुकता), *Overdue* (देर)।
- **Pending Invoices** = Unpaid + Partial।

### 6.2 एडवांस (Advance) — ग्राहक का पहले का भुगतान
ग्राहक जब पहले पैसे देता है (मरम्मत के बिल बनने से पहले):
- **Payments → Add Payment**: दिशा = *Received*, रकम, तरीका (Cash/UPI…), और
  **"Advance" टिक** करें। इससे रकम ग्राहक के **Advance Balance** (पूल) में जमा हो जाती है।

### 6.3 एडवांस को बिल पर लागू करना (Apply Advance)
बिल खुला हो और ग्राहक का एडवांस पूल में पड़ा हो, तो बिल पेज पर एक मेसेज दिखता है:
> "Customer has ₹____ unapplied advance — **Apply ₹____ to this invoice**"

- **Apply ₹__** दबाएँ → एडवांस बिल पर लग जाता है, Balance Due घट जाता है।
- गलती हो तो **Un-apply** से एडवांस वापस पूल में चला जाता है।
- मरम्मत से बिल बनते समय यह अपने-आप हो जाता है — अलग से दबाने की ज़रूरत नहीं।

**उदाहरण (Suvam Giri):** एडवांस ₹1000, बिल ₹7000 → Apply करने पर Balance Due ₹6000 →
फिर ₹6000 कैश लें → बिल *Paid*। कुल ₹7000, डुप्लीकेट नहीं।

### 6.4 बाकी भुगतान लेना (Balance Payment)
**Payments → Add Payment**: दिशा = *Received*, ग्राहक चुनें, रकम भरें।
Advance **टिक न करें** (यह साधारण भुगतान है)। बिल से जोड़ने पर Balance अपने-आप घटेगा।

---

## 7. कैश और बैंक (Cash / Bank)

### 7.1 कैश बुक (Cash Book)
मेन्यू → **Cash Book** (`/cash-book/`) — रोज़ का कैश इन्फ/आउट रजिस्टर।
डैशबोर्ड का **Cash in Hand** कार्ड इसी को खोलता है।

### 7.2 बैंक खाते (Bank Accounts)
मेन्यू → **Bank** (`/bank/accounts/`) — हर खाते का बैलेंस। **Bank Balance** कार्ड।

### 7.3 कैश ⇄ बैंक जमा/निकास (Contra Entry)
- कैश से बैंक में जमा, या बैंक से कैश निकालना = **Contra** entry।
- Cash Book / Bank पेज से Contra फ़ॉर्म खोलें, रकम और खाता चुनें।
- ध्यान: UPI/बैंक का भुगतान लेते समय ledger उसी method के अनुसार बनता है।

---

## 8. खर्चे / Expenses और Chart of Accounts

दुकान का हर खर्चा Ledger में एक "expense account" में जाता है। अब खर्चे दो समूहों में बँटे हैं
(Tally की तरह):

- **Direct Expenses (समूह 51):** सीधे माल/काम से जुड़े खर्च — Purchases, Wages,
  Carriage Inward, Factory बिजली, Freight, Import Duty आदि।
  → ये **Trading Account** में जाते हैं और **Gross Profit** निकालते हैं।
- **Indirect Expenses (समूह 52):** दुकान/ऑफिस के आम खर्च — Shop Rent, Staff Salary,
  Electricity, Phone/Internet, Bank Charges, Insurance, Bad Debt आदि।
  → ये **Profit & Loss Account** में जाते हैं और **Net Profit** निकालते हैं।

रोज़ का खर्चा दर्ज करने के दो तरीके:
1. सामान खरीदना → **Purchases** (§5) बनाना (सबसे सही)।
2. कोई और खर्चा (बिजली का बिल, किराया) → **Journal** entry या **Payments** (दिशा = *Paid*),
   सही expense account चुनें (51 या 52 समूह से)।

> Accounts/GROUP बदलना admin/owner का काम है; स्टाफ सिर्फ़ सही account चुने।

---

## 9. जर्नल (Journal)

मेन्यू → **Journals** (`/journals/`) — अकाउंटेंट के लिए manual entries
(discount, adjustment, कोई खास खर्चा)। आम बिक्री/खरीद/भुगतान यहाँ से नहीं,
ऊपर वाले मॉड्यूल्स से होंगे।

---

## 10. रिपोर्ट्स (Reports)

मेन्यू → **Reports**:
- **Sales** — बिक्री रिपोर्ट
- **Purchases** — खरीद रिपोर्ट
- **GST** — GST हिसाब
- **Stock** — स्टॉक रिपोर्ट
- **Aging** — पुराने बकाया (कितने दिन से बाकी)
- **Profit & Loss** — नीचे (§11)
- **Trial Balance / Balance Sheet** — पूरा खाता संतुलन
हर रिपोर्ट पर **Excel** और **Print** बटन हैं; ऊपर date range दे सकते हैं।

---

## 11. Profit & Loss रिपोर्ट (Trading और P&L अलग-अलग)

Reports → **Profit & Loss** (`/reports/profit-loss/`) अब दो हिस्सों में दिखती है:

**Trading Account (मोटा मुनाफ़ा / Gross Profit)**
```
Sales + Repair Income − Direct Expenses (समूह 51) = GROSS PROFIT
```
**Profit & Loss Account (शुद्ध मुनाफ़ा / Net Profit)**
```
Gross Profit + Other Income − Indirect Expenses (समूह 52) = NET PROFIT
```
- ऊपर date range चुनें — सिर्फ़ उन्हीं तारीखों का लेखा-जोखा गिना जाएगा।
- Gross Profit हरा (+) और Net Profit लाल (−) रंग में नुकसान दिखाते हैं।
- Excel/Print पर यही दो-हिस्से वाली शक्ल आती है।

---

## 12. आम नियम (स्टाफ के लिए ज़रूरी)

1. ग्राहक/वेंडर/प्रोडक्ट **पहले बनाएँ**, फिर बिल/भुगतान में चुनें।
2. एडवांस लेते समय **Advance टिक** करें — बिल पर बाद में Apply करें।
3. सामान खरीदने पर **Purchase** बनाएँ (Adjustment से बचें)।
4. कैश/बैंक की गड़बड़ी हो तो **Cash Book** और **Bank Balance** से मिलाएँ।
5. कुछ गलत दर्ज हो गया → पहले **Un-apply / Delete** का विकल्प देखें, फिर दोबारा दर्ज करें।
6. डैशबोर्ड का कोई नंबर समझ न आए → कार्ड पर क्लिक करके पूरी लिस्ट देखें।

---

## 13. तेज़ काम (Cheat Sheet)

| काम | कहाँ से |
|---|---|
| नया ग्राहक | Contacts → Add |
| नया प्रोडक्ट + स्टॉक | Products → Add (Current Stock) |
| स्टॉक सुधार | Stock → Adjustments |
| मरम्मत दर्ज | Repairs → Add |
| एडवांस लेना | Payments → Add (Advance ✓) |
| एडवांस बिल पर लगाना | Invoice → Apply ₹__ |
| बिल बनाना | Sales → Add / Repair → Create Invoice |
| बाकी भुगतान | Payments → Add (Advance ✗) |
| सामान खरीद | Purchases → Add |
| कैश जमा/निकास | Cash Book → Contra |
| मुनाफ़ा देखना | Reports → Profit & Loss |

# Phase 3: Technical Levels & Limit Orders (تعیین سطوح تکنیکال و اردرگذاری پله‌ای)

```yaml
phase_id: "PHASE_3_TECHNICAL_LEVELS"
name_en: "Technical Levels & Order Planning"
name_fa: "سطوح تکنیکال و برنامه‌ریزی سفارش‌های ورود"
steps:
  - INPUT_CHART_METRICS
  - CALCULATE_LIMIT_ORDERS
  - SET_INVALIDATION
next_phase: "PHASE_4_WEEKLY_AUTOPILOT"
```

---

## 1. Step: `INPUT_CHART_METRICS`

### هدف
دریافت سطوح کلیدی قیمتی از چارت هفتگی/روزانه برای دارایی‌های فعال در پورتفوی.

### رفتار ایجنت
- برای دارایی‌های تعریف‌شده در لایه هسته و لایه رشد، سطوح قیمتی کلیدی زیر را استعلام کنید:
  1. **قیمت فعلی بازار (Current Price)**
  2. **حمایت اول / تست سطح شکسته‌شده (Support 1 / Pullback Level)**
  3. **حمایت ماژور / کف رنج هفتگی (Support 2 / Major Range Low)**
  4. **کف نهایی / اکستریم ماکرو (Extreme Discount Level)**
- در صورتی که تعداد دارایی‌ها چند مورد است، مرحله به مرحله برای دارایی‌های اصلی یا در قالب یک لیست مشخص بپرسید.

### قالب سوال به فارسی
```text
برای تنظیم سفارش‌های پله‌ای نماد «{symbol}»، لطفاً سطوح کلیدی زیر را از چارت هفتگی/روزانه ارسال کنید:
• قیمت فعلی (Market Price):
• سطح حمایت اول (مناسب پله ۱):
• سطح حمایت دوم / ماژور (مناسب پله ۲):
• سطح اکستریم یا کف رنج قیمتی (مناسب پله ۳):
```

### تغییرات در `state.json`
- ذخیره سطوح تکنیکال دارایی در ساختار پوزیشن‌های موقت.
- `current_step`: `"CALCULATE_LIMIT_ORDERS"`.

---

## 2. Step: `CALCULATE_LIMIT_ORDERS`

### هدف
تلفیق تخصیص ریالی/دلاری بودجه پورتفوی با درصدهای پله‌ها (`entry_tranches`) و سطوح قیمتی جهت تولید اردرهای لیمیت خرید دقیق.

### محاسبات ریاضی
- **حجم پله ۱**: `Tranche 1 Allocation = Total Asset Allocation * tranche_1_pct / 100`
- **حجم پله ۲**: `Tranche 2 Allocation = Total Asset Allocation * tranche_2_pct / 100`
- **حجم پله ۳**: `Tranche 3 Allocation = Total Asset Allocation * tranche_3_pct / 100`
- **قیمت میانگین خرید بالقوه (Weighted Average Price)** در صورت پر شدن تمام پله‌ها.

### رفتار ایجنت
- جدول دقیق اردرهای خرید لیمیت را به صورت شفاف و تفکیک‌شده نمایش دهید.
- شامل: نماد، شماره پله، قیمت سفارش، درصد سرمایه، و مبلغ دقیق به ارز مبنا.
- از کاربر تایید بگیرید.

### قالب سوال به فارسی
```text
جدول سفارش‌های لیمیت پوزیشن تریدینگ برای «{symbol}»:

• پله ۱ (۳۰٪ = {tranche_1_amt} {currency}): خرید لیمیت در قیمت {price_1}
• پله ۲ (۴۰٪ = {tranche_2_amt} {currency}): خرید لیمیت در قیمت {price_2}
• پله ۳ (۳۰٪ = {tranche_3_amt} {currency}): خرید لیمیت در قیمت {price_3}
• میانگین موزون در صورت فعال‌سازی کامل: {avg_entry_price}

آیا این جدول سفارش را تایید می‌کنید؟ (پاسخ دهید «تایید»)
```

### تغییرات در `state.json`
- افزودن به آرایه `positions`:
  ```json
  {
    "symbol": "{symbol}",
    "layer": "{layer}",
    "allocated_budget": 0,
    "tranches": [
      {"tranche": 1, "pct": 30, "price": 0, "amount": 0, "status": "PENDING"},
      {"tranche": 2, "pct": 40, "price": 0, "amount": 0, "status": "PENDING"},
      {"tranche": 3, "pct": 30, "price": 0, "amount": 0, "status": "PENDING"}
    ],
    "average_entry": null,
    "invalidation_price": null,
    "status": "PLANNING"
  }
  ```
- `current_step`: `"SET_INVALIDATION"`.

---

## 3. Step: `SET_INVALIDATION`

### هدف
تعیین سطح بی‌اعتباری ساختار (Invalidation / Hard Stop) و شروط خروج اضطراری.

### منطق پوزیشن تریدینگ
- در پوزیشن تریدینگ، حد ضرر نوسانی و چسبیده به قیمت نداریم؛ اما هر ساختار تحلیلی یک نقطه بی‌اعتباری قطعی در تایم‌فریم هفتگی دارد (مانند شکست تثبیت‌شده کف تاریخی یا تغییر بنیادین متغیرهای کلان).
- سطح قیمت یا شرط کیفی بی‌اعتباری ثبت می‌شود.

### رفتار ایجنت
- با توجه به سطوح پله ۳، سطح پیشنهادی خروج اضطراری (بستن زیر کف هفتگی با تلورانس ۳ الی ۵ درصد) را مطرح کنید.
- از کاربر تایید یا مقدار عددی مد نظر را بگیرید.

### قالب سوال به فارسی
```text
سطح بی‌اعتباری ساختار کلان (Invalidation Level) برای «{symbol}»:
پیشنهاد سیستم: بسته شدن کندل هفتگی زیر قیمت {suggested_invalidation} (فاصله {distance_pct}٪ از پله ۳).

آیا این سطح را تایید می‌کنید یا قیمت متفاوتی در نظر دارید؟ (ارسال «تایید» یا ثبت قیمت عددی)
```

### تغییرات در `state.json`
- به‌روزرسانی `invalidation_price` در شیء مربوط به دارایی داخل `positions`.
- تغییر وضعیت پوزیشن به `"ACTIVE_PENDING_ORDERS"`.
- در صورت تکمیل تمام دارایی‌های مد نظر:
  - `current_phase`: `"PHASE_4_WEEKLY_AUTOPILOT"`.
  - `current_step`: `"WEEKLY_DEEP_SEARCH"`.

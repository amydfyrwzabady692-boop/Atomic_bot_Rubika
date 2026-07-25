# Atomic Rubika Bot

نسخه مستقل فروشگاه اتومیک برای روبیکا. این مخزن، دیتابیس، کانتینرها،
توکن و تنظیمات جداگانه دارد و هیچ وابستگی اجرایی به ربات تلگرام ندارد.

## امکانات

- منوی کاربر: جم، سنسیویتی موبایل و PC، فروشگاه، کیف پول، سفارش‌ها،
  حساب و پشتیبانی
- پنل مدیر: آمار، کاربران، سفارش‌ها، رسیدها، محصولات، دسته‌بندی‌ها،
  کد هدیه و تخفیف، مدیران، دپارتمان‌ها، تنظیمات مالی، پیام همگانی و
  جوین اجباری با تأیید مدیر
- زرین‌پال با لینک مستقیم قابل کپی و callback امن HTTPS
- کارت‌به‌کارت سفارش و شارژ کیف پول با نمایش مجدد تصویر رسید در پنل و
  تأیید دومرحله‌ای مدیر
- کیف پول با دفتر کل، پرداخت کامل/جزئی، بازگشت تراکنشی وجه و کنترل مغایرت
- تحویل جم G2Bulk فقط پس از ثبت قطعی پرداخت
- تحویل مدیریتی پک سنس و محصولات فروشگاه فقط پس از ثبت قطعی پرداخت
- رزرو موجودی محصول و آزادسازی خودکار سفارش‌های پرداخت‌نشده
- بررسی مجدد محدود پرداخت‌های زرین‌پال در صورت نرسیدن callback
- نرخ زنده USDT/تومان و ثبت snapshot سود هر سفارش
- Webhook تولیدی و Long Polling برای راه‌اندازی اولیه
- Docker Compose، PostgreSQL، healthcheck و اجرای non-root

## ساخت بات روبیکا

1. در روبیکا وارد `@BotFather` شوید و یک بات تازه بسازید.
2. توکن بات را فقط در `.env` این پروژه قرار دهید.
3. ابتدا `RUBIKA_MODE=polling` بگذارید تا بدون دامنه شناسه‌ها را به دست آورید.
4. به بات `/start` بفرستید و از لاگ رویداد، `sender_id` و `chat_id` مدیر را
   به‌ترتیب در `RUBIKA_ADMIN_ID` و `RUBIKA_ADMIN_CHAT_ID` قرار دهید.
5. پس از آماده‌شدن دامنه HTTPS، حالت را به `webhook` تغییر دهید.

API رسمی: https://rubika.ir/botapi

## اجرا با Docker

```bash
cp .env.example .env
nano .env
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 bot
curl -fsS http://127.0.0.1:8081/ready
```

برای production، Nginx باید دامنه `rubika.atomicshop.ir` را به
`127.0.0.1:8081` پراکسی کند. callback زرین‌پال:

```text
https://rubika.atomicshop.ir/payment/callback
```

## فرمان‌های مدیر

ورود: `/admin`

```text
/product_add kind|title|price|stock|amount|sku|cost_usd
/product_edit id|field|value
/product_delete id
/category_add title
/category_delete id
/code_add gift|CODE|VALUE|MAX
/code_add discount|CODE|PERCENT|MAX
/code_delete id
/user RUBIKA_ID
/users_balance
/users_referral
/users_card
/block USER_DB_ID
/unblock USER_DB_ID
/order ID
/charge USER_ID AMOUNT
/charge_all AMOUNT
/broadcast TEXT
/reply TICKET_ID TEXT
/receipt_ok ID
/receipt_no ID
/join_ok USER_DB_ID
/join_no USER_DB_ID
/admin_add RUBIKA_ID TITLE
/admin_delete RUBIKA_ID
/setting card_number NUMBER
/setting card_holder NAME
/setting card_bank BANK
/setting welcome_text TEXT
/department_add TITLE
/department_delete ID
/channel_add CHAT_ID|TITLE|INVITE_URL
/channel_delete ID
```

مقادیر `kind` مجاز: `gem`، `sense_mobile`، `sense_pc` و `store`.

## نکات مالی

- مبلغ callback از URL کاربر پذیرفته نمی‌شود؛ مبلغ و authority فقط از
  رکورد قفل‌شده دیتابیس خوانده و سپس مستقیماً با زرین‌پال verify می‌شود.
- authority و ref_id یکتا هستند و callback تکراری اثر مالی دوباره ندارد.
- هر سفارش فقط یک پرداخت فعال دارد؛ ساخت روش پرداخت جدید، قبلی را لغو می‌کند.
- اگر لینک قدیمی زرین‌پال واقعاً پرداخت شده باشد، نتیجه مستقیم درگاه دوباره
  بررسی می‌شود و تا وقتی سفارش پرداخت نشده است قابل ثبت خواهد بود.
- رسید کارت‌به‌کارت به‌تنهایی پرداخت محسوب نمی‌شود و حتماً باید مدیر آن را
  در دو مرحله تأیید کند.
- تحویل G2Bulk فقط برای سفارش `paid` اجرا می‌شود و کلید idempotency ثابت دارد.

## محدودیت جوین اجباری روبیکا

Bot API رسمی روبیکا در نسخه فعلی متد عمومی بررسی عضویت یک کاربر مشخص در
کانال را ارائه نمی‌کند. برای جلوگیری از تأیید جعلی، درخواست کاربر به مدیر
ارسال و با `/join_ok` یا `/join_no` بررسی می‌شود.

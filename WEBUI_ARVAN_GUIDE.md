# انتشار پنل با Caddy پشت آروان

```text
Browser → HTTPS :443 → Arvan → HTTP :8080 → Caddy → 127.0.0.1:8787
```

پنل Python فقط روی loopback می‌ماند. Caddy با کاربر اختصاصی `ikev2-proxy`
و بدون capability یا دسترسی root اجرا می‌شود و فقط اتصال‌های IPهای آروان
و loopback را می‌پذیرد. هدرهای جعلی IP این محدودیت را دور نمی‌زنند.
پورت‌های 80 و 443 اشغال نمی‌شوند؛ تمدید گواهی VPN تغییری نمی‌کند.

**ارتباط آروان تا سرور HTTP است**؛ رمز و نشست در این بخش رمزگذاری نمی‌شوند.
HTTPS مرورگر فقط بخش مرورگر تا آروان را پوشش می‌دهد. گواهی و کلید آمادهٔ
سرور برای این حالت لازم نیستند و نباید داخل مخزن یا چت قرار بگیرند.

## تنظیم آروان

برای رکورد همین دامنه، ابر روشن و مبدأ `155.117.13.45` باشد:

| تنظیم | مقدار |
| --- | --- |
| پروتکل مبدأ | HTTP |
| پورت مبدأ | 8080 |
| HTTPS سمت بازدیدکننده | فعال، با گواهی معتبر دامنه |
| Host ارسالی به مبدأ | p.mahanfreenet.ir |
| X-Forwarded-Proto | پروتکل واقعی بازدیدکننده، توسط آروان |
| کش | خاموش برای تمام مسیرهای این دامنه |

قانون اختصاصی `p.mahanfreenet.ir/*` را بالاتر از قانون‌های عمومی بگذار.
پس از خاموش‌کردن کش، کش قدیمی همین دامنه را پاک کن. پاسخ‌های API و ورود
نباید `X-Cache: HIT` یا `STALE` داشته باشند. Caddy هم `Cache-Control:
private, no-store` می‌فرستد؛ خاموش‌کردن کش در CDN همچنان لازم است.

## استقرار با workflow

در GitHub Actions، workflow با نام **Deploy Production Web UI** را اجرا کن:

- `publish_arvan`: روشن.
- `inspect_origin`: خاموش.
- `verify_public_only`: خاموش؛ برای بررسی بدون نصب آن را روشن کن.
- `reset_admin_password`: خاموش، مگر اینکه عمداً بازنشانی رمز بخواهی.

فایل `packaging/arvan-origin.json` دامنه و پورت را مشخص می‌کند.
تنظیم فعال در `/etc/ikev2-web-proxy/origin.json`، با مالک root است و
کاربر پنل اجازهٔ تغییر آن را ندارد. نصب‌کننده با root، باینری Caddy را از
بستهٔ احرازشدهٔ APT استخراج می‌کند؛ سرویس پیش‌فرض بسته نصب یا اجرا نمی‌شود.
سرویس اختصاصی `ikev2-web-proxy.service` پس از راه‌اندازی مجدد سرور هم
خودکار شروع می‌شود.

فهرست جاری شبکه‌های آروان از نشانی رسمی HTTPS دریافت و اعتبارسنجی می‌شود.
هر استقرار بعدی Web UI، تا وقتی فایل `origin.json` موجود است، باینری و
فهرست IP را تازه و پروکسی را بازراه‌اندازی می‌کند. اجرای عادی APT به‌تنهایی
این کپی اختصاصی Caddy را به‌روزرسانی نمی‌کند.

نصب‌کننده کوکی امن HTTPS را با حفظ رمز و تنظیمات حساب فعال می‌کند.
بازراه‌اندازی پنل نشست‌های فعلی را تمام می‌کند؛ دوباره از HTTPS وارد شو.
پس از این تغییر، ورود از تونل HTTP مرورگر ممکن است کار نکند.

قواعد فایروال، NAT، DNS و سرویس VPN توسط این نصب تغییر نمی‌کنند.
اگر فایروال ورودی 8080 را مسدود کند، فقط TCP این پورت از شبکه‌های آروان
باید مجاز شود. پورت عمومی 8787 لازم نیست.

## کنترل سرویس روی سرور

از کامپیوتر خود وارد سرور شو:

```bash
ssh -p 9011 saeed@155.117.13.45
```

فرمان‌های زیر روی `srv-nl` اجرا می‌شوند. وضعیت:

```bash
sudo systemctl status ikev2-web-proxy.service --no-pager
```

شروع:

```bash
sudo systemctl start ikev2-web-proxy.service
```

توقف:

```bash
sudo systemctl stop ikev2-web-proxy.service
```

بازراه‌اندازی:

```bash
sudo systemctl restart ikev2-web-proxy.service
```

این فرمان‌ها پروکسی را کنترل می‌کنند. نام سرویس خود پنل `ikev2-web.service`
است؛ توقف پروکسی به سرویس VPN فرمانی نمی‌فرستد.

## بررسی

روی سرور، پنل داخلی:

```bash
curl --fail http://127.0.0.1:8787/api/health
```

روی سرور، عبور از Caddy:

```bash
curl --fail -H 'Host: p.mahanfreenet.ir' -H 'X-Forwarded-Proto: https' http://127.0.0.1:8080/api/health
```

از کامپیوتر، مسیر کامل CDN:

```bash
curl --fail -i https://p.mahanfreenet.ir/api/health
```

ورود دستی را در مرورگر روی `https://p.mahanfreenet.ir` بررسی کن.
کوکی باید `Secure`، `HttpOnly` و `SameSite=Strict` باشد. داشبورد بدون
ورود باید 401 بدهد. اتصال مستقیم از اینترنت به `http://155.117.13.45:8080`
باید 403 بدهد یا توسط فایروال مسدود شود. تست workflow رمز را به CDN نمی‌فرستد.
بررسی عمومی workflow از خود سرور، با آدرس HTTPS عمومی و عبور از CDN انجام
می‌شود. محدودیت‌های منطقه‌ای یا امنیتی CDN ممکن است نتیجه را از شبکه‌های
دیگر متفاوت کنند؛ ورود مرورگر خودت را هم بررسی کن.
اگر آروان خودِ درخواست آزمون را با صفحهٔ HTML و خطای 403 مسدود کند،
workflow هشدار می‌دهد و بررسی عمومی را انجام‌شده حساب نمی‌کند؛ این حالت
استقرار سالم مبدأ را ناموفق نمی‌کند. پاسخ 403 خود Caddy و خطاهای دیگر
همچنان باعث شکست بررسی می‌شوند. محدودیت کشور یا IP آروان تغییر نمی‌کند.

محدودیت فعلی: شمارندهٔ خطای ورود IP اتصال مستقیم را می‌بیند؛ پشت پروکسی
خطاهای کاربران در شمارندهٔ loopback جمع می‌شوند. پس از پنج تلاش ناموفق
ممکن است ورود همه تا پنج دقیقه محدود شود.

## غیرفعال‌کردن انتشار

برای جلوگیری از فعال‌شدن دوباره در استقرارهای بعدی، روی سرور:

```bash
sudo mv /etc/ikev2-web-proxy/origin.json /etc/ikev2-web-proxy/origin.disabled.json
```

```bash
sudo systemctl disable --now ikev2-web-proxy.service
```

برای برگشت کوکی به حالت HTTP تونل محلی، با حفظ رمز:

```bash
cd /opt/ikev2
```

```bash
sudo python3 -B -m webui.manage configure-cookie --config /etc/ikev2-web/config.json --allow-http
```

```bash
sudo systemctl restart ikev2-web.service
```

اگر قاعدهٔ فایروال اختصاصی ساختی، پس از توقف انتشار آن را هم حذف کن.
فایل احراز هویت پنل و سرویس‌های VPN را حذف نکن.

## مستندات

- [شبکه‌های رسمی آروان](https://www.arvancloud.ir/fa/dev/ips)
- [HTTPS و پروتکل مبدأ](https://docs.arvancloud.ir/fa/cdn/https-settings/)
- [هدرهای آروان](https://docs.arvancloud.ir/fa/cdn/headers/)
- [کش آروان](https://docs.arvancloud.ir/fa/cdn/caching/)
- [قانون‌های صفحه](https://docs.arvancloud.ir/fa/cdn/page-rules/)
- [remote_ip در Caddy](https://caddyserver.com/docs/caddyfile/matchers#remote_ip)

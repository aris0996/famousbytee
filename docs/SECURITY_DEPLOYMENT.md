# Security deployment checklist

1. Generate new independent values for `SECRET_KEY`, `JWT_SECRET_KEY`, database password, the Si Dobe secret key, and any legacy provider secrets still required for migration.
2. Put them in the production environment using `.env.example` as the variable list. Never commit `.env`.
   For Si Dobe set the `SIDOBE_API_KEY`/stored Si Dobe Secret Key and use `WHATSAPP_PROVIDER=sidobe`. Do not expose or re-enable the legacy WAHA adapter in the operator UI.
3. Change the database user's password on the database server, then update `DB_PASS` atomically.
4. Configure the Si Dobe webhook at `/webhooks/sidobe` in the Si Dobe Console. It must send `X-Webhook-Signature`; the signature is SHA-256 of `SecretKey|webhook_id`, using the same Si Dobe Secret Key used for API calls.
5. Apply `docs/apache-security.conf.example` in the Apache VirtualHost and restart Apache.
6. Deploy the application, restart every WSGI worker, then log in again because rotating session/JWT secrets invalidates existing sessions.
7. Purge the historical `config.py` secret from Git history if this repository has ever been shared, and rotate credentials again after the purge.
8. Verify `/robots.txt`, `/.well-known/security.txt`, `/sitemap.xml`, security headers, CORS, login throttling, and CSRF after deployment.

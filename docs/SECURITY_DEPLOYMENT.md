# Security deployment checklist

1. Generate new independent values for `SECRET_KEY`, `JWT_SECRET_KEY`, database password, and `SIDOBE_WEBHOOK_SECRET`.
2. Put them in the production environment using `.env.example` as the variable list. Never commit `.env`.
3. Change the database user's password on the database server, then update `DB_PASS` atomically.
4. Configure Si Dobe to send `X-Webhook-Secret` with the same `SIDOBE_WEBHOOK_SECRET` value.
5. Apply `docs/apache-security.conf.example` in the Apache VirtualHost and restart Apache.
6. Deploy the application, restart every WSGI worker, then log in again because rotating session/JWT secrets invalidates existing sessions.
7. Purge the historical `config.py` secret from Git history if this repository has ever been shared, and rotate credentials again after the purge.
8. Verify `/robots.txt`, `/.well-known/security.txt`, `/sitemap.xml`, security headers, CORS, login throttling, and CSRF after deployment.

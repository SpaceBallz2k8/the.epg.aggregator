from flask import request, abort, make_response, render_template

def init_security(app, db, BannedIP, WHITELIST_IPS, get_current_user_func):
    """
    Initializes security middleware and error handlers.
    """

    @app.before_request
    def block_banned_ips():
        ip = request.remote_addr
        if ip in WHITELIST_IPS:
            return
        # Check database for ban
        ban = BannedIP.query.filter_by(ip=ip).first()
        if ban:
            return abort(403)

    def auto_ban(reason):
        ip = request.remote_addr
        if ip in WHITELIST_IPS:
            return
        if not BannedIP.query.filter_by(ip=ip).first():
            new_ban = BannedIP(ip=ip, reason=reason)
            db.session.add(new_ban)
            db.session.commit()
            print(f"!!! SECURITY: Auto-banned {ip}. Reason: {reason}")

    @app.errorhandler(400)
    def handle_bad_request(e):
        """
        Protocol violations or malformed requests.
        Excludes common TLS handshake attempts on HTTP port from auto-ban.
        """
        error_message = str(e)
        # Check for common patterns of TLS handshakes on HTTP port
        # These often appear as "Bad request version" with binary data
        if "Bad request version" in error_message and (
            "\\x16\\x03\\x01" in error_message or # TLSv1.0/1.1/1.2 ClientHello
            "\\x16\\x03\\x02" in error_message or # TLSv1.1 ClientHello
            "\\x16\\x03\\x03" in error_message    # TLSv1.2 ClientHello
        ):
            print(f"!!! SECURITY: Ignored potential TLS handshake from {request.remote_addr}")
            return make_response("Bad Request", 400) # Don't ban, just return 400
        auto_ban(f"Protocol violation: {error_message}")
        return make_response("Bad Request", 400)


    @app.errorhandler(404)
    def handle_not_found(e):
        """
        Zero Tolerance: Bans any IP requesting a non-existent route.
        """
        path = request.path
        # Exclude common browser/crawler requests from the Zero Tolerance ban
        if path not in ['/favicon.ico', '/robots.txt', '/sitemap.xml']:
            auto_ban(f"Invalid route requested: {path}")
        # Show a generic index for the user (if they are legitimate) 
        # though they'll be banned instantly if they aren't whitelisted.
        return render_template('index.html', user=get_current_user_func()), 404

    @app.errorhandler(405)
    def handle_not_allowed(e):
        """Ban unusual HTTP methods (e.g. POST to a GET route)."""
        auto_ban(f"Method not allowed on {request.path}")
        return make_response("Method Not Allowed", 405)

    @app.errorhandler(505)
    def handle_version_not_supported(e):
        """Ban HTTP/2.0 or 3.0 probes on this HTTP/1.1 server."""
        auto_ban("Invalid HTTP version probe")
        return make_response("HTTP Version Not Supported", 505)

    print("🛡️ Security system initialized with Zero Tolerance policy.")
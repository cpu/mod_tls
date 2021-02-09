/* Copyright 2021, ISRG (https://www.abetterinternet.org)
 *
 * This software is licensed as described in the file LICENSE, which
 * you should have received as part of this distribution.
 *
 */
#include <assert.h>
#include <apr_optional.h>
#include <apr_strings.h>

#include <mpm_common.h>
#include <httpd.h>
#include <http_core.h>
#include <http_connection.h>
#include <http_log.h>
#include <http_protocol.h>

#include <crustls.h>

#include "mod_tls.h"
#include "tls_defs.h"
#include "tls_conf.h"
#include "tls_core.h"
#include "tls_filter.h"
#include "tls_version.h"

static void tls_hooks(apr_pool_t *pool);

AP_DECLARE_MODULE(tls) = {
    STANDARD20_MODULE_STUFF,
    NULL,                  /* create per dir config */
    NULL,                  /* merge per dir config */
    tls_conf_create_svr,   /* create per server config */
    tls_conf_merge_svr,    /* merge per server config (inheritance) */
    tls_conf_cmds,         /* command handlers */
    tls_hooks,
#if defined(AP_MODULE_FLAG_NONE)
    AP_MODULE_FLAG_ALWAYS_MERGE
#endif
};

static const char* crustls_version(apr_pool_t *p)
{
    char buffer[1024];
    size_t len;

    memset(buffer, 0, sizeof(buffer));
    len = rustls_version(buffer, sizeof(buffer)-1);
    return apr_pstrndup(p, buffer, len);
}

static apr_status_t tls_post_config(apr_pool_t *p, apr_pool_t *plog,
                                    apr_pool_t *ptemp, server_rec *s)
{
    const char *tls_init_key = "mod_tls_init_counter";
    int dry_run = 0;
    void *data = NULL;

    (void)p;
    (void)plog;

    apr_pool_userdata_get(&data, tls_init_key, s->process->pool);
    if (data == NULL) {
        /* At the first start, httpd makes a config check dry run
        * to see if the config is ok in principle.
         */
        dry_run = 1;
        ap_log_error(APLOG_MARK, APLOG_TRACE1, 0, s,
                     APLOGNO() "post config dry run");
        apr_pool_userdata_set((const void *)1, tls_init_key,
                              apr_pool_cleanup_null, s->process->pool);
    }
    else {
        ap_log_error(APLOG_MARK, APLOG_INFO, 0, s, APLOGNO()
                     "mod_tls (v%s, crustls=%s), initializing...",
                     MOD_TLS_VERSION, crustls_version(ptemp));
    }

    return tls_core_init(p, ptemp, s);
}

static int hook_pre_connection(conn_rec *c, void *csd)
{
    tls_conf_conn_t *cc;
    int rv = DECLINED;

    (void)csd; /* mpm specific socket data, not used */
    /* are we on a primary connection and configured for it?
     * Then attach a tls_conf_conn_t to it. */
    if (c->master) goto cleanup;
    cc = tls_conf_conn_get(c);
    if (cc && cc->flag_disabled == TLS_FLAG_TRUE) goto cleanup;

    rv = tls_core_conn_init(c);
    if (OK != rv) goto cleanup;

    /* TODO: this is where mod_ssl runs its 'pre_handshake' hook.
     * this allows mod_reqtimeout to monitor the handshake duration
     * and abort on a stall by the client. */

    /* Install out input/output filters for handling connection data */
    rv = tls_filter_conn_init(c);

cleanup:
    return rv;
}

static int hook_connection(conn_rec* c)
{
    tls_conf_conn_t *cc = tls_conf_conn_get(c);

    if (cc && cc->rustls_session) {
        /* Send the initialization signal down the filter chain.
         * This will start the TLS handshake. */
        apr_bucket_brigade* temp = apr_brigade_create(c->pool, c->bucket_alloc);
        ap_get_brigade(c->input_filters, temp, AP_MODE_INIT, APR_BLOCK_READ, 0);
        apr_brigade_destroy(temp);
    }
    /* we do *not* take over. others may add to connection processing, e.g.
     * the core http or the http2 connection handler. */
    return DECLINED;
}


static void tls_hooks(apr_pool_t *pool)
{
    /* If our request check denies further processing, certain things
     * need to be in place for the response to be correctly generated. */
    static const char *pre_req_check[] = { "mod_setenvif.c", NULL };

    ap_log_perror(APLOG_MARK, APLOG_TRACE1, 0, pool, "installing hooks");
    /* TODO: the order that config hooks run determines the order in which
     * vital filters are installed. There are challenges:
     * - some modules need to run before and/or after mod_ssl. they probably
     *   need to run before/after mod_tls as well.
     * - coexistence: if mod_ssl is loaded as well, does in matter where
     *   mod_tls runs in relation to it?
     */
    tls_filter_register(pool);

    ap_hook_post_config(tls_post_config, NULL, NULL, APR_HOOK_MIDDLE);
    /* connection things */
    ap_hook_pre_connection(hook_pre_connection, NULL, NULL, APR_HOOK_MIDDLE);
    ap_hook_process_connection(hook_connection, NULL, NULL, APR_HOOK_MIDDLE);
    /* request things */
    ap_hook_post_read_request(tls_core_request_check, pre_req_check, NULL, APR_HOOK_MIDDLE);
}

/* Copyright 2021, ISRG (https://www.abetterinternet.org)
 *
 * This software is licensed as described in the file LICENSE, which
 * you should have received as part of this distribution.
 *
 */

#include <assert.h>
#include <apr_lib.h>
#include <apr_strings.h>
#include <apr_network_io.h>

#include <httpd.h>
#include <http_core.h>
#include <http_log.h>

#include "tls_defs.h"
#include "tls_conf.h"
#include "tls_core.h"
#include "tls_util.h"


static int we_listen_on(tls_conf_global_t *gc, server_rec *s)
{
    server_addr_rec *sa, *la;

    for (la = gc->tls_addresses; la; la = la->next) {
        for (sa = s->addrs; sa; sa = sa->next) {
            if (la->host_port == sa->host_port
                && la->host_addr->ipaddr_len == sa->host_addr->ipaddr_len
                && !memcmp(la->host_addr->ipaddr_ptr,
                    la->host_addr->ipaddr_ptr, (size_t)la->host_addr->ipaddr_len)) {
                /* exact match */
                return 1;
            }
        }
    }
    return 0;
}

static apr_status_t tls_core_free(void *data)
{
    server_rec *base_server = (server_rec *)data;
    server_rec *s;
    tls_conf_server_t *sc;

    /* free all rustls things we are owning. */
    for (s = base_server; s; s = s->next) {
        sc = tls_conf_server_get(s);
        if (sc) {
            if (sc->rustls_config) {
                rustls_server_config_free(sc->rustls_config);
                sc->rustls_config = NULL;
            }
        }
    }

    return APR_SUCCESS;
}

apr_status_t tls_core_init(apr_pool_t *p, apr_pool_t *ptemp, server_rec *base_server)
{
    tls_conf_server_t *sc = tls_conf_server_get(base_server);
    tls_conf_global_t *gc = sc->global;
    server_rec *s;
    rustls_server_config_builder *rustls_builder;
    apr_status_t rv = APR_ENOMEM;
    rustls_result rr = RUSTLS_RESULT_OK;
    const char *err_descr;

    apr_pool_cleanup_register(p, base_server, tls_core_free,
                              apr_pool_cleanup_null);

    for (s = base_server; s; s = s->next) {
        sc = tls_conf_server_get(s);
        if (!sc) continue;
        ap_assert(sc->global == gc);

        /* If 'TLSListen' has been configured, use those addresses to
         * decide if we are enabled on this server.
         * If not, auto-enable when 'https' is set as protocol.
         * This is done via the apache 'Listen <port> https' directive. */
        if (gc->tls_addresses) {
            sc->enabled = we_listen_on(gc, s)? TLS_FLAG_TRUE : TLS_FLAG_FALSE;
        }
        else if (sc->enabled == TLS_FLAG_UNSET
            && ap_get_server_protocol(s)
            && strcmp("https", ap_get_server_protocol(s)) == 0) {
            sc->enabled = TLS_FLAG_TRUE;
        }
        /* otherwise, we always fallback to disabled */
        if (sc->enabled == TLS_FLAG_UNSET) {
            sc->enabled = TLS_FLAG_FALSE;
        }
    }

    /* Collect and prepare certificates for enabled servers */

    /* Create server configs for enabled servers */
    for (s = base_server; s; s = s->next) {
        sc = tls_conf_server_get(s);
        if (!sc || sc->enabled != TLS_FLAG_TRUE) continue;

        rustls_builder = rustls_server_config_builder_new();
        if (!rustls_builder) goto cleanup;

        /* TODO: not yet available
        rr = rustls_server_config_builder_load_native_roots(rustls_builder);
        if (rr != RUSTLS_RESULT_OK) {
            rv = tls_util_rustls_error(ptemp, rr, &err_descr);
            ap_log_error(APLOG_MARK, APLOG_ERR, rv, s, APLOGNO()
                         "Failed to load local roots for server %s: %s",
                         s->server_hostname, err_descr);
            goto cleanup;
        }
        */
        /* TODO: this needs some more work */
        if (sc->certificates->nelts > 0) {
            tls_certificate_t *spec = APR_ARRAY_IDX(sc->certificates, 0, tls_certificate_t*);
            tls_util_cert_pem_t *pems;

            rv = tls_util_load_pem(ptemp, spec, &pems);
            if (APR_SUCCESS != rv) {
                ap_log_error(APLOG_MARK, APLOG_ERR, rv, s, APLOGNO()
                             "Failed to load certficate for server %s",
                             s->server_hostname);
                goto cleanup;
            }

            rr = rustls_server_config_builder_set_single_cert_pem(rustls_builder,
                pems->cert_pem_bytes, pems->cert_pem_len,
                pems->key_pem_bytes, pems->key_pem_len);
            if (rr != RUSTLS_RESULT_OK) {
                rv = tls_util_rustls_error(ptemp, rr, &err_descr);
                ap_log_error(APLOG_MARK, APLOG_ERR, rv, s, APLOGNO()
                             "Failed to load certficates for server %s: %s",
                             s->server_hostname, err_descr);
                goto cleanup;
            }
        }

        sc->rustls_config = rustls_server_config_builder_build(rustls_builder);
        if (!sc->rustls_config) goto cleanup;
    }

    rv = APR_SUCCESS;
cleanup:
    return rv;
}


int tls_core_conn_init(conn_rec *c)
{
    tls_conf_conn_t *cc = tls_conf_conn_get(c);
    tls_conf_server_t *sc = tls_conf_server_get(c->base_server);
    int rv = DECLINED;

    /* Are we configured to work on this address/port? */
    if (sc->enabled != TLS_FLAG_TRUE) goto cleanup;

    cc = apr_pcalloc(c->pool, sizeof(*cc));
    /* start with the base server, SNI may update this during handshake */
    cc->s = c->base_server;
    rv = OK;
cleanup:
    return rv;
}


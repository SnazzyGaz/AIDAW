/* pdclap: minimal offline CLAP host for PlainDAW (GPL-3.0-or-later).
 * Loads a .clap bundle, instantiates one plugin and processes audio + note/param
 * events in blocks. Single-threaded: "main thread" outside process(), "audio thread"
 * inside. Exposed as a plain C API for Python ctypes. */
#include <clap/clap.h>
#ifdef _WIN32
#include <windows.h>
#define PD_API __declspec(dllexport)
static char pd_dlerr[64];
static void *dlopen(const char *p, int f) { (void)f; void *h = (void *)LoadLibraryA(p); if (!h) snprintf(pd_dlerr, sizeof pd_dlerr, "LoadLibrary error %lu", GetLastError()); return h; }
static void *dlsym(void *h, const char *s) { return (void *)GetProcAddress((HMODULE)h, s); }
static int dlclose(void *h) { return FreeLibrary((HMODULE)h) ? 0 : -1; }
static const char *dlerror(void) { return pd_dlerr; }
#define RTLD_NOW 0
#define RTLD_LOCAL 0
#else
#include <dlfcn.h>
#define PD_API __attribute__((visibility("default")))
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

typedef struct {
    uint16_t type;     /* 0 note on, 1 note off, 2 param value */
    uint32_t time;
    int32_t key;       /* note key or param id */
    int32_t channel;
    double value;      /* velocity 0..1 or param plain value */
} pd_event;

typedef struct pdc {
    void *lib;
    const clap_plugin_entry_t *entry;
    const clap_plugin_t *plugin;
    clap_host_t host;
    const clap_plugin_params_t *params;
    const clap_plugin_state_t *state;
    const clap_plugin_audio_ports_t *ports;
    const clap_plugin_latency_t *latency;
    int in_process;
    int callback_requested;
    int activated;
    int processing;
    uint32_t n_in_ports, n_out_ports;
    uint32_t in_ch[8], out_ch[8];
    /* event buffers */
    union { clap_event_note_t note; clap_event_param_value_t param; } *evs;
    uint32_t n_evs, cap_evs;
    int64_t steady;
    char err[512];
} pdc;

/* ---------- host callbacks ---------- */
static int tc_main(const clap_host_t *h) { return !((pdc *)h->host_data)->in_process; }
static int tc_audio(const clap_host_t *h) { return ((pdc *)h->host_data)->in_process; }
static bool b_tc_main(const clap_host_t *h) { return tc_main(h); }
static bool b_tc_audio(const clap_host_t *h) { return tc_audio(h); }
static const clap_host_thread_check_t host_tc = { b_tc_main, b_tc_audio };

static void h_log(const clap_host_t *h, clap_log_severity s, const char *msg) {
    (void)h; if (s >= CLAP_LOG_WARNING) fprintf(stderr, "[clap] %s\n", msg);
}
static const clap_host_log_t host_log = { h_log };

static void hp_rescan(const clap_host_t *h, clap_param_rescan_flags f) { (void)h; (void)f; }
static void hp_clear(const clap_host_t *h, clap_id id, clap_param_clear_flags f) { (void)h; (void)id; (void)f; }
static void hp_flush(const clap_host_t *h) { (void)h; }
static const clap_host_params_t host_params = { hp_rescan, hp_clear, hp_flush };

static void hs_dirty(const clap_host_t *h) { (void)h; }
static const clap_host_state_t host_state = { hs_dirty };
static void hl_changed(const clap_host_t *h) { (void)h; }
static const clap_host_latency_t host_latency = { hl_changed };
static bool hap_supported(const clap_host_t *h, uint32_t f) { (void)h; (void)f; return false; }
static void hap_rescan(const clap_host_t *h, uint32_t f) { (void)h; (void)f; }
static const clap_host_audio_ports_t host_aports = { hap_supported, hap_rescan };

static const void *h_get_ext(const clap_host_t *h, const char *id) {
    (void)h;
    if (!strcmp(id, CLAP_EXT_THREAD_CHECK)) return &host_tc;
    if (!strcmp(id, CLAP_EXT_LOG)) return &host_log;
    if (!strcmp(id, CLAP_EXT_PARAMS)) return &host_params;
    if (!strcmp(id, CLAP_EXT_STATE)) return &host_state;
    if (!strcmp(id, CLAP_EXT_LATENCY)) return &host_latency;
    if (!strcmp(id, CLAP_EXT_AUDIO_PORTS)) return &host_aports;
    return NULL;
}
static void h_req_restart(const clap_host_t *h) { (void)h; }
static void h_req_process(const clap_host_t *h) { (void)h; }
static void h_req_callback(const clap_host_t *h) { ((pdc *)h->host_data)->callback_requested = 1; }

/* ---------- event lists ---------- */
static uint32_t in_size(const clap_input_events_t *l) { return ((pdc *)l->ctx)->n_evs; }
static const clap_event_header_t *in_get(const clap_input_events_t *l, uint32_t i) {
    pdc *p = (pdc *)l->ctx; return i < p->n_evs ? &p->evs[i].note.header : NULL;
}
static bool out_push(const clap_output_events_t *l, const clap_event_header_t *e) { (void)l; (void)e; return true; }

static void service_main(pdc *p) {
    if (p->callback_requested && p->plugin->on_main_thread) {
        p->callback_requested = 0;
        p->plugin->on_main_thread(p->plugin);
    }
}

/* ---------- API ---------- */
PD_API const char *pdc_last_error(pdc *p) { return p ? p->err : "null handle"; }

/* Write descriptor list "id\tname\tvendor\tversion\tfeatures\n" into buf. Returns count or -1. */
PD_API int pdc_list(const char *path, char *buf, int cap) {
    void *lib = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!lib) { snprintf(buf, cap, "%s", dlerror()); return -1; }
    const clap_plugin_entry_t *entry = (const clap_plugin_entry_t *)dlsym(lib, "clap_entry");
    if (!entry || !entry->init(path)) { snprintf(buf, cap, "no clap_entry"); dlclose(lib); return -1; }
    const clap_plugin_factory_t *f = (const clap_plugin_factory_t *)entry->get_factory(CLAP_PLUGIN_FACTORY_ID);
    int n = f ? (int)f->get_plugin_count(f) : 0;
    buf[0] = 0; int used = 0;
    for (int i = 0; i < n; i++) {
        const clap_plugin_descriptor_t *d = f->get_plugin_descriptor(f, i);
        char feats[256] = ""; int fl = 0;
        for (const char *const *ft = d->features; ft && *ft; ft++)
            fl += snprintf(feats + fl, sizeof(feats) - fl, "%s%s", fl ? "," : "", *ft);
        used += snprintf(buf + used, cap - used, "%s\t%s\t%s\t%s\t%s\n", d->id, d->name,
                         d->vendor ? d->vendor : "", d->version ? d->version : "", feats);
    }
    entry->deinit();
    dlclose(lib);
    return n;
}

PD_API pdc *pdc_load(const char *path, const char *plugin_id) {
    pdc *p = calloc(1, sizeof(pdc));
    p->lib = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!p->lib) { snprintf(p->err, sizeof p->err, "dlopen: %s", dlerror()); return p; }
    p->entry = (const clap_plugin_entry_t *)dlsym(p->lib, "clap_entry");
    if (!p->entry || !p->entry->init(path)) { snprintf(p->err, sizeof p->err, "bad clap_entry"); return p; }
    const clap_plugin_factory_t *f = (const clap_plugin_factory_t *)p->entry->get_factory(CLAP_PLUGIN_FACTORY_ID);
    if (!f || f->get_plugin_count(f) == 0) { snprintf(p->err, sizeof p->err, "no plugins in factory"); return p; }
    const char *id = plugin_id && *plugin_id ? plugin_id : f->get_plugin_descriptor(f, 0)->id;
    p->host.clap_version = (clap_version_t)CLAP_VERSION_INIT;
    p->host.host_data = p;
    p->host.name = "PlainDAW"; p->host.vendor = "PlainDAW"; p->host.url = ""; p->host.version = "0.1";
    p->host.get_extension = h_get_ext;
    p->host.request_restart = h_req_restart;
    p->host.request_process = h_req_process;
    p->host.request_callback = h_req_callback;
    p->plugin = f->create_plugin(f, &p->host, id);
    if (!p->plugin) { snprintf(p->err, sizeof p->err, "create_plugin failed for %s", id); return p; }
    if (!p->plugin->init(p->plugin)) { snprintf(p->err, sizeof p->err, "plugin init failed"); p->plugin->destroy(p->plugin); p->plugin = NULL; return p; }
    p->params = p->plugin->get_extension(p->plugin, CLAP_EXT_PARAMS);
    p->state = p->plugin->get_extension(p->plugin, CLAP_EXT_STATE);
    p->ports = p->plugin->get_extension(p->plugin, CLAP_EXT_AUDIO_PORTS);
    p->latency = p->plugin->get_extension(p->plugin, CLAP_EXT_LATENCY);
    if (p->ports) {
        p->n_in_ports = p->ports->count(p->plugin, true);
        p->n_out_ports = p->ports->count(p->plugin, false);
        if (p->n_in_ports > 8) p->n_in_ports = 8;
        if (p->n_out_ports > 8) p->n_out_ports = 8;
        for (uint32_t i = 0; i < p->n_in_ports; i++) { clap_audio_port_info_t inf; p->ports->get(p->plugin, i, true, &inf); p->in_ch[i] = inf.channel_count; }
        for (uint32_t i = 0; i < p->n_out_ports; i++) { clap_audio_port_info_t inf; p->ports->get(p->plugin, i, false, &inf); p->out_ch[i] = inf.channel_count; }
    }
    p->cap_evs = 1024;
    p->evs = calloc(p->cap_evs, sizeof(*p->evs));
    service_main(p);
    return p;
}

PD_API int pdc_ok(pdc *p) { return p && p->plugin != NULL; }
PD_API int pdc_ports(pdc *p, int input, int idx) { /* channel count of port, -1 if none */
    if (input) return idx < (int)p->n_in_ports ? (int)p->in_ch[idx] : -1;
    return idx < (int)p->n_out_ports ? (int)p->out_ch[idx] : -1;
}
PD_API int pdc_latency(pdc *p) { return (p->latency && p->activated) ? (int)p->latency->get(p->plugin) : 0; }

PD_API int pdc_activate(pdc *p, double sr, int max_frames) {
    if (!pdc_ok(p)) return 0;
    if (!p->plugin->activate(p->plugin, sr, 1, (uint32_t)max_frames)) { snprintf(p->err, sizeof p->err, "activate failed"); return 0; }
    p->activated = 1;
    service_main(p);
    return 1;
}

PD_API int pdc_param_count(pdc *p) { return p->params ? (int)p->params->count(p->plugin) : 0; }
/* returns id; fills name/module, min, max, default */
PD_API int pdc_param_info(pdc *p, int index, char *name, int ncap, double *mn, double *mx, double *def, uint32_t *flags) {
    clap_param_info_t inf;
    if (!p->params || !p->params->get_info(p->plugin, (uint32_t)index, &inf)) return -1;
    snprintf(name, ncap, "%s%s%s", inf.module, inf.module[0] ? "/" : "", inf.name);
    *mn = inf.min_value; *mx = inf.max_value; *def = inf.default_value; *flags = inf.flags;
    return (int)inf.id;
}
PD_API int pdc_param_get(pdc *p, uint32_t id, double *v) { return p->params ? p->params->get_value(p->plugin, id, v) : 0; }

static void fill_events(pdc *p, const pd_event *ev, int n) {
    if ((uint32_t)n > p->cap_evs) { p->cap_evs = n; p->evs = realloc(p->evs, n * sizeof(*p->evs)); }
    p->n_evs = n;
    for (int i = 0; i < n; i++) {
        memset(&p->evs[i], 0, sizeof(p->evs[i]));
        if (ev[i].type == 2) {
            clap_event_param_value_t *e = &p->evs[i].param;
            e->header.size = sizeof(*e); e->header.time = ev[i].time; e->header.space_id = CLAP_CORE_EVENT_SPACE_ID;
            e->header.type = CLAP_EVENT_PARAM_VALUE;
            e->param_id = (clap_id)ev[i].key; e->cookie = NULL;
            e->note_id = -1; e->port_index = -1; e->channel = -1; e->key = -1; e->value = ev[i].value;
        } else {
            clap_event_note_t *e = &p->evs[i].note;
            e->header.size = sizeof(*e); e->header.time = ev[i].time; e->header.space_id = CLAP_CORE_EVENT_SPACE_ID;
            e->header.type = ev[i].type == 0 ? CLAP_EVENT_NOTE_ON : CLAP_EVENT_NOTE_OFF;
            e->note_id = -1; e->port_index = 0; e->channel = (int16_t)ev[i].channel; e->key = (int16_t)ev[i].key; e->velocity = ev[i].value;
        }
    }
}

/* Set params while not processing (uses params.flush). */
PD_API int pdc_flush_params(pdc *p, const pd_event *ev, int n) {
    if (!p->params || !p->params->flush) return 0;
    fill_events(p, ev, n);
    clap_input_events_t in = { p, in_size, in_get };
    clap_output_events_t out = { p, out_push };
    p->params->flush(p->plugin, &in, &out);
    p->n_evs = 0;
    return 1;
}

/* Process one block. in/out are planar float: [ch][frames]. in_ch/out_ch are the
 * channel counts provided by the caller for the main ports. Returns clap status or -1. */
PD_API int pdc_process(pdc *p, const float *in, int in_ch, float *out, int out_ch, int frames, const pd_event *ev, int n_ev) {
    if (!pdc_ok(p) || !p->activated) return -1;
    if (!p->processing) {
        p->in_process = 1;
        if (!p->plugin->start_processing(p->plugin)) { p->in_process = 0; snprintf(p->err, sizeof p->err, "start_processing failed"); return -1; }
        p->processing = 1;
        p->in_process = 0;
    }
    fill_events(p, ev, n_ev);
    clap_audio_buffer_t ib[8], ob[8];
    float *iptr[8][8], *optr[8][8];
    static float zero[8][8192]; static float scratch[8][8][8192];
    if (frames > 8192) return -1;
    for (uint32_t i = 0; i < p->n_in_ports; i++) {
        for (uint32_t c = 0; c < p->in_ch[i] && c < 8; c++) {
            if (i == 0 && in && (int)c < in_ch) iptr[i][c] = (float *)(in + (size_t)c * frames);
            else if (i == 0 && in && in_ch > 0) iptr[i][c] = (float *)(in + (size_t)(in_ch - 1) * frames);
            else iptr[i][c] = zero[c];
        }
        ib[i].data32 = iptr[i]; ib[i].data64 = NULL; ib[i].channel_count = p->in_ch[i]; ib[i].latency = 0; ib[i].constant_mask = 0;
    }
    for (uint32_t i = 0; i < p->n_out_ports; i++) {
        for (uint32_t c = 0; c < p->out_ch[i] && c < 8; c++)
            optr[i][c] = (i == 0 && (int)c < out_ch) ? out + (size_t)c * frames : scratch[i][c];
        ob[i].data32 = optr[i]; ob[i].data64 = NULL; ob[i].channel_count = p->out_ch[i]; ob[i].latency = 0; ob[i].constant_mask = 0;
    }
    clap_input_events_t inl = { p, in_size, in_get };
    clap_output_events_t outl = { p, out_push };
    clap_process_t pr;
    memset(&pr, 0, sizeof pr);
    pr.steady_time = p->steady; pr.frames_count = (uint32_t)frames; pr.transport = NULL;
    pr.audio_inputs = ib; pr.audio_outputs = ob;
    pr.audio_inputs_count = p->n_in_ports; pr.audio_outputs_count = p->n_out_ports;
    pr.in_events = &inl; pr.out_events = &outl;
    p->in_process = 1;
    clap_process_status st = p->plugin->process(p->plugin, &pr);
    p->in_process = 0;
    p->steady += frames;
    p->n_evs = 0;
    /* main port mono -> duplicate to stereo request */
    if (p->n_out_ports > 0 && p->out_ch[0] == 1 && out_ch > 1) memcpy(out + frames, out, frames * sizeof(float));
    service_main(p);
    return (int)st;
}

typedef struct { char *buf; size_t len, cap, pos; } membuf;
static int64_t os_write(const clap_ostream_t *s, const void *b, uint64_t n) {
    membuf *m = (membuf *)s->ctx;
    if (m->len + n > m->cap) { m->cap = (m->len + n) * 2; m->buf = realloc(m->buf, m->cap); }
    memcpy(m->buf + m->len, b, n); m->len += n; return (int64_t)n;
}
static int64_t is_read(const clap_istream_t *s, void *b, uint64_t n) {
    membuf *m = (membuf *)s->ctx;
    uint64_t left = m->len - m->pos; if (n > left) n = left;
    memcpy(b, m->buf + m->pos, n); m->pos += n; return (int64_t)n;
}
/* Save state into a malloc'd buffer; returns size, -1 on failure. Free with pdc_free. */
PD_API long pdc_state_save(pdc *p, char **out) {
    if (!p->state) return -1;
    membuf m = { NULL, 0, 0, 0 };
    clap_ostream_t os = { &m, os_write };
    if (!p->state->save(p->plugin, &os)) { free(m.buf); return -1; }
    *out = m.buf; return (long)m.len;
}
PD_API int pdc_state_load(pdc *p, const char *data, long n) {
    if (!p->state) return 0;
    membuf m = { (char *)data, (size_t)n, (size_t)n, 0 };
    clap_istream_t is = { &m, is_read };
    int ok = p->state->load(p->plugin, &is);
    service_main(p);
    return ok;
}
PD_API void pdc_free(void *ptr) { free(ptr); }

PD_API void pdc_destroy(pdc *p) {
    if (!p) return;
    if (p->plugin) {
        if (p->processing) { p->in_process = 1; p->plugin->stop_processing(p->plugin); p->in_process = 0; }
        if (p->activated) p->plugin->deactivate(p->plugin);
        p->plugin->destroy(p->plugin);
    }
    if (p->entry) p->entry->deinit();
    /* do not dlclose: some plugins leave threads behind */
    free(p->evs);
    free(p);
}

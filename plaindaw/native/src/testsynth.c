/* PlainDAW test CLAP plugins (GPL-3.0-or-later): a deterministic sine synth
 * "org.plaindaw.testsynth" and a gain effect "org.plaindaw.testgain" whose
 * "crash" parameter deliberately segfaults (used to test the plugin sandbox). */
#include <clap/clap.h>
#include <math.h>
#include <string.h>
#include <stdlib.h>
#include <stdio.h>

typedef struct { clap_plugin_t plugin; const clap_host_t *host; double sr; int is_synth;
  double gain_db; double crash; double phase[128]; int on[128]; double vel[128]; double env[128]; } tp;

static bool p_init(const clap_plugin_t *pl) { (void)pl; return true; }
static void p_destroy(const clap_plugin_t *pl) { free(pl->plugin_data); }
static bool p_activate(const clap_plugin_t *pl, double sr, uint32_t a, uint32_t b) { (void)a; (void)b; ((tp *)pl->plugin_data)->sr = sr; return true; }
static void p_deactivate(const clap_plugin_t *pl) { (void)pl; }
static bool p_start(const clap_plugin_t *pl) { (void)pl; return true; }
static void p_stop(const clap_plugin_t *pl) { (void)pl; }
static void p_reset(const clap_plugin_t *pl) { (void)pl; }
static void p_omt(const clap_plugin_t *pl) { (void)pl; }

static void handle(tp *t, const clap_event_header_t *h) {
  if (h->space_id != CLAP_CORE_EVENT_SPACE_ID) return;
  if (h->type == CLAP_EVENT_NOTE_ON) { const clap_event_note_t *n = (const void *)h; if (n->key >= 0 && n->key < 128) { t->on[n->key] = 1; t->vel[n->key] = n->velocity; } }
  else if (h->type == CLAP_EVENT_NOTE_OFF) { const clap_event_note_t *n = (const void *)h; if (n->key >= 0 && n->key < 128) t->on[n->key] = 0; }
  else if (h->type == CLAP_EVENT_PARAM_VALUE) { const clap_event_param_value_t *e = (const void *)h;
    if (e->param_id == 1) t->gain_db = e->value;
    if (e->param_id == 2) t->crash = e->value; }
}

static clap_process_status p_process(const clap_plugin_t *pl, const clap_process_t *pr) {
  tp *t = pl->plugin_data;
  uint32_t ne = pr->in_events->size(pr->in_events), ei = 0;
  float **out = pr->audio_outputs[0].data32;
  for (uint32_t i = 0; i < pr->frames_count; i++) {
    while (ei < ne) { const clap_event_header_t *h = pr->in_events->get(pr->in_events, ei); if (h->time > i) break; handle(t, h); ei++; }
    if (t->crash > 0.5) { volatile int *bad = NULL; *bad = 42; }
    double g = pow(10.0, t->gain_db / 20.0);
    double l, r;
    if (t->is_synth) {
      double s = 0;
      for (int k = 0; k < 128; k++) {
        double target = t->on[k] ? t->vel[k] : 0.0;
        t->env[k] += (target - t->env[k]) * 0.002;
        if (t->env[k] > 1e-5) { s += sin(t->phase[k]) * t->env[k] * 0.3; t->phase[k] += 2 * M_PI * 440.0 * pow(2.0, (k - 69) / 12.0) / t->sr; if (t->phase[k] > 2 * M_PI) t->phase[k] -= 2 * M_PI; }
      }
      l = r = s * g;
    } else {
      l = pr->audio_inputs[0].data32[0][i] * g;
      r = pr->audio_inputs[0].data32[pr->audio_inputs[0].channel_count > 1 ? 1 : 0][i] * g;
    }
    out[0][i] = (float)l; out[1][i] = (float)r;
  }
  return CLAP_PROCESS_CONTINUE;
}

/* params */
static uint32_t pa_count(const clap_plugin_t *pl) { (void)pl; return 2; }
static bool pa_info(const clap_plugin_t *pl, uint32_t i, clap_param_info_t *inf) {
  (void)pl; memset(inf, 0, sizeof *inf);
  if (i == 0) { inf->id = 1; strcpy(inf->name, "Gain"); inf->min_value = -60; inf->max_value = 12; inf->default_value = 0; }
  else if (i == 1) { inf->id = 2; strcpy(inf->name, "Crash"); inf->min_value = 0; inf->max_value = 1; inf->default_value = 0; inf->flags = CLAP_PARAM_IS_STEPPED; }
  else return false;
  inf->flags |= CLAP_PARAM_IS_AUTOMATABLE; return true;
}
static bool pa_get(const clap_plugin_t *pl, clap_id id, double *v) { tp *t = pl->plugin_data; if (id == 1) { *v = t->gain_db; return true; } if (id == 2) { *v = t->crash; return true; } return false; }
static bool pa_v2t(const clap_plugin_t *pl, clap_id id, double v, char *o, uint32_t n) { (void)pl; snprintf(o, n, id == 1 ? "%.1f dB" : "%.0f", v); return true; }
static bool pa_t2v(const clap_plugin_t *pl, clap_id id, const char *s, double *v) { (void)pl; (void)id; *v = atof(s); return true; }
static void pa_flush(const clap_plugin_t *pl, const clap_input_events_t *in, const clap_output_events_t *out) {
  (void)out; tp *t = pl->plugin_data; for (uint32_t i = 0; i < in->size(in); i++) handle(t, in->get(in, i)); }
static const clap_plugin_params_t ext_params = { pa_count, pa_info, pa_get, pa_v2t, pa_t2v, pa_flush };

/* state */
static bool st_save(const clap_plugin_t *pl, const clap_ostream_t *s) { tp *t = pl->plugin_data; return s->write(s, &t->gain_db, sizeof(double)) == sizeof(double); }
static bool st_load(const clap_plugin_t *pl, const clap_istream_t *s) { tp *t = pl->plugin_data; return s->read(s, &t->gain_db, sizeof(double)) == sizeof(double); }
static const clap_plugin_state_t ext_state = { st_save, st_load };

/* audio ports */
static uint32_t ap_count(const clap_plugin_t *pl, bool is_in) { tp *t = pl->plugin_data; return (is_in && t->is_synth) ? 0 : 1; }
static bool ap_get(const clap_plugin_t *pl, uint32_t i, bool is_in, clap_audio_port_info_t *inf) {
  (void)pl; if (i) return false; memset(inf, 0, sizeof *inf); inf->id = is_in ? 0 : 1; strcpy(inf->name, is_in ? "In" : "Out");
  inf->flags = CLAP_AUDIO_PORT_IS_MAIN; inf->channel_count = 2; inf->port_type = CLAP_PORT_STEREO; inf->in_place_pair = CLAP_INVALID_ID; return true; }
static const clap_plugin_audio_ports_t ext_ports = { ap_count, ap_get };

static const void *p_ext(const clap_plugin_t *pl, const char *id) {
  (void)pl;
  if (!strcmp(id, CLAP_EXT_PARAMS)) return &ext_params;
  if (!strcmp(id, CLAP_EXT_STATE)) return &ext_state;
  if (!strcmp(id, CLAP_EXT_AUDIO_PORTS)) return &ext_ports;
  return NULL;
}

static const char *f_synth[] = { CLAP_PLUGIN_FEATURE_INSTRUMENT, CLAP_PLUGIN_FEATURE_SYNTHESIZER, NULL };
static const char *f_fx[] = { CLAP_PLUGIN_FEATURE_AUDIO_EFFECT, NULL };
static const clap_plugin_descriptor_t d_synth = { CLAP_VERSION_INIT, "org.plaindaw.testsynth", "PlainDAW Test Synth", "PlainDAW", "", "", "", "0.1", "test sine synth", f_synth };
static const clap_plugin_descriptor_t d_fx = { CLAP_VERSION_INIT, "org.plaindaw.testgain", "PlainDAW Test Gain", "PlainDAW", "", "", "", "0.1", "test gain with crash switch", f_fx };

static const clap_plugin_t *create(const clap_host_t *host, const clap_plugin_descriptor_t *d, int synth) {
  tp *t = calloc(1, sizeof(tp)); t->host = host; t->is_synth = synth;
  t->plugin.desc = d; t->plugin.plugin_data = t;
  t->plugin.init = p_init; t->plugin.destroy = p_destroy; t->plugin.activate = p_activate; t->plugin.deactivate = p_deactivate;
  t->plugin.start_processing = p_start; t->plugin.stop_processing = p_stop; t->plugin.reset = p_reset;
  t->plugin.process = p_process; t->plugin.get_extension = p_ext; t->plugin.on_main_thread = p_omt;
  return &t->plugin;
}
static uint32_t fa_count(const clap_plugin_factory_t *f) { (void)f; return 2; }
static const clap_plugin_descriptor_t *fa_desc(const clap_plugin_factory_t *f, uint32_t i) { (void)f; return i == 0 ? &d_synth : i == 1 ? &d_fx : NULL; }
static const clap_plugin_t *fa_create(const clap_plugin_factory_t *f, const clap_host_t *h, const char *id) {
  (void)f; if (!strcmp(id, d_synth.id)) return create(h, &d_synth, 1); if (!strcmp(id, d_fx.id)) return create(h, &d_fx, 0); return NULL; }
static const clap_plugin_factory_t factory = { fa_count, fa_desc, fa_create };
static bool e_init(const char *p) { (void)p; return true; }
static void e_deinit(void) {}
static const void *e_factory(const char *id) { return !strcmp(id, CLAP_PLUGIN_FACTORY_ID) ? &factory : NULL; }
CLAP_EXPORT const clap_plugin_entry_t clap_entry = { CLAP_VERSION_INIT, e_init, e_deinit, e_factory };

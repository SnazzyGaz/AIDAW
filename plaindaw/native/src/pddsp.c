/* Small DSP kernels for PlainDAW (optional: Python fallbacks exist). Public domain-ish, GPLv3 with the project. */
#include <math.h>
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#ifdef _WIN32
#define PD_API __declspec(dllexport)
#else
#define PD_API
#endif

static void coeffs(int kind, double f0, double sr, double q, double gain_db, double *b, double *a) {
    if (f0 < 10.0) f0 = 10.0;
    if (f0 > sr * 0.49) f0 = sr * 0.49;
    double A = pow(10.0, gain_db / 40.0);
    double w = 2.0 * M_PI * f0 / sr, cw = cos(w), sw = sin(w);
    double alpha = sw / (2.0 * (q > 1e-3 ? q : 1e-3));
    double a0;
    switch (kind) {
    case 0: /* lowpass */
        b[0] = (1 - cw) / 2; b[1] = 1 - cw; b[2] = (1 - cw) / 2; a0 = 1 + alpha; a[1] = -2 * cw; a[2] = 1 - alpha; break;
    case 1: /* highpass */
        b[0] = (1 + cw) / 2; b[1] = -(1 + cw); b[2] = (1 + cw) / 2; a0 = 1 + alpha; a[1] = -2 * cw; a[2] = 1 - alpha; break;
    case 2: /* bandpass */
        b[0] = alpha; b[1] = 0; b[2] = -alpha; a0 = 1 + alpha; a[1] = -2 * cw; a[2] = 1 - alpha; break;
    default: /* peak */
        b[0] = 1 + alpha * A; b[1] = -2 * cw; b[2] = 1 - alpha * A; a0 = 1 + alpha / A; a[1] = -2 * cw; a[2] = 1 - alpha / A; break;
    }
    b[0] /= a0; b[1] /= a0; b[2] /= a0; a[1] /= a0; a[2] /= a0;
}

/* x, y: (ch, n) row-major. freq: n values. Coefficients refreshed every `block` samples. */
PD_API void pd_tvbiquad(const double *x, double *y, int ch, long n, const double *freq, int kind, double q, double sr,
                 double gain_db, int block) {
    double z1[16] = {0}, z2[16] = {0};
    double b[3], a[3];
    if (ch > 16) ch = 16;
    if (block < 1) block = 1;
    for (long s = 0; s < n; s += block) {
        long e = s + block < n ? s + block : n;
        long mid = s + block / 2 < n ? s + block / 2 : n - 1;
        coeffs(kind, freq[mid], sr, q, gain_db, b, a);
        for (int c = 0; c < ch; c++) {
            const double *xi = x + (long)c * n;
            double *yo = y + (long)c * n;
            double s1 = z1[c], s2 = z2[c];
            for (long i = s; i < e; i++) {
                double in = xi[i];
                double out = b[0] * in + s1;
                s1 = b[1] * in - a[1] * out + s2;
                s2 = b[2] * in - a[2] * out;
                yo[i] = out;
            }
            z1[c] = s1; z2[c] = s2;
        }
    }
}

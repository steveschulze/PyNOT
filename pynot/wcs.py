from astropy.wcs import WCS
from astropy.io import fits
from astropy.table import Table
from astropy.utils.exceptions import AstropyWarning

import numpy as np
import matplotlib.pyplot as plt
import warnings
import os


def update_WCS(coords, refs, crval, CD):
    # Solve equations:
    X_obs = list()
    X_ref = list()
    for obs, ref in zip(coords, refs):
        x0, y0 = obs
        xr, yr = ref
        # For x:
        X_obs.append([1., 0., x0, y0, 0., 0.])
        X_ref.append(xr)
        # For y:
        X_obs.append([0., 1., 0., 0., x0, y0])
        X_ref.append(yr)

    X_obs = np.array(X_obs)
    X_ref = np.array(X_ref)

    p_opt = np.linalg.lstsq(X_obs, X_ref, rcond=None)[0]
    cx, cy, a, b, c, d = p_opt

    R = np.array([[a, b], [c, d]])
    offset = np.array([cx, cy])

    # Calculate updated solution:
    crval_new = offset + R.dot(crval)
    CD_new = R.dot(CD)
    wcs_new = {
        'CRVAL1': crval_new[0],
        'CRVAL2': crval_new[1],
        'CD1_1': CD_new[0, 0],
        'CD1_2': CD_new[0, 1],
        'CD2_1': CD_new[1, 0],
        'CD2_2': CD_new[1, 1]}
    return wcs_new


def mad(x):
    return np.median(np.abs(x-np.median(x)))


def get_WCS(hdr):
    p0 = np.array([hdr['CRVAL1'], hdr['CRVAL2']])
    x0 = np.array([hdr['CRPIX1'], hdr['CRPIX2']])
    CD = np.array([[hdr['CD1_1'], hdr['CD1_2']],
                   [hdr['CD2_1'], hdr['CD2_2']]])
    proj = np.array([np.cos(p0[1]/180*np.pi), 1.])
    return x0, p0, CD, proj


def pixel_to_radec(coords_pix, header):
    # -- Convert SEP pixel coordinates to WCS using header
    x0, p0, CD, proj = get_WCS(header)
    tmp = coords_pix - (x0-1)
    coords = p0 + CD.dot(tmp.T).T / proj
    return coords


def match_catalogs(coords, refs):
    matched = list()
    for xy in coords:
        index = np.argmin(np.sum((refs - xy)**2, axis=1))
        matched.append(refs[index])
    return np.array(matched)


def get_gaia_catalog(ra, dec, radius=4., limit=200, catalog_fname='', database='edr3'):
    """
    ra and dec: units of degrees
    radius: units of arcmin
    limit: max number of targets to retrieve
    """
    from astroquery.gaia import Gaia
    query_args = {'limit': limit, 'ra': ra, 'dec': dec, 'radius': radius/60., 'dr': database}
    query = """SELECT TOP {limit} ra, dec, phot_g_mean_mag FROM gaia{dr}.gaia_source
    WHERE CONTAINS(POINT('ICRS', gaia{dr}.gaia_source.ra, gaia{dr}.gaia_source.dec),
                   CIRCLE('ICRS', {ra}, {dec}, {radius}))=1;""".format(**query_args)
    job = Gaia.launch_job_async(query, dump_to_file=True,
                                output_format='csv', verbose=False,
                                output_file=catalog_fname)
    result = job.get_results()
    return result




def correct_wcs(img_fname, sep_fname, output_fname='', fig_fname='', max_num=60, min_num=10, G_lim=15, q_lim=0.8, kappa=3):
    """
    WCS calibration using Gaia

    Sources are matched using the initial WCS solution from the header. Outliers are rejected
    based on projected distance. The WCS solution is then matched to match the Gaia positions.

    Parameters
    ----------
    img_fname : string
        ALFOSC image filename (.fits)

    sep_fname : string
        Filename of the source extraction table (_phot.fits)

    output_fname : string
        Filename of the WCS corrected image. Autogenerated by default.

    fig_fname : string
        Filename of the diagnostic figure. Autogenerated by default.

    max_num : int  [default=60]
        Maximum number of targets used to fit the WCS solution.

    min_num : int  [default=10]
        Minimum number of targets required.

    G_lim : float  [default=15]
        Bright limit of Gaia sources. Reject targets brighter than G < G_lim as these are
        very likely saturated in the ALFOSC image.

    q_lim : float  [default=0.8]
        Reject elliptical sources with axis ratio < `q_lim`.
        Axis ratio is defined as minor/major.

    kappa : float  [default=3]
        Threshold for projected distance filtering. Sources are rejected if the distance differs
        more then `kappa` times the median absolute deviation from the median of all distances.

    Returns
    -------
    output_msg : string
        Log of messages from the function call.
    """
    msg = list()

    img = fits.getdata(img_fname)
    hdr = fits.getheader(img_fname)
    msg.append("          - Loaded image: %s" % img_fname)

    # Prepare output filenames:
    base, ext = os.path.splitext(os.path.basename(img_fname))
    dirname = os.path.dirname(img_fname)
    if output_fname == '':
        output_fname = img_fname
        # output_fname = base + '_wcs' + ext
        # output_fname = os.path.join(dirname, output_fname)

    if fig_fname == '':
        fig_fname = 'wcs_solution_' + base + '.pdf'
        fig_fname = os.path.join(dirname, fig_fname)

    image_radius = np.sqrt(hdr['NAXIS1']**2 + hdr['NAXIS2']**2) / 2
    image_scale = np.sqrt(hdr['CD1_1']**2 + hdr['CD1_2']**2)
    deg_to_arcmin = 60.
    radius = image_scale * image_radius * deg_to_arcmin
    gaia_cat_name = 'gaia_source_%.2f%+.2f_%.1f.csv' % (hdr['CRVAL1'], hdr['CRVAL2'], radius)
    gaia_cat_name = os.path.join(dirname, gaia_cat_name)
    gaia_dr = 'edr3'
    if os.path.exists(gaia_cat_name):
        ref_cat = Table.read(gaia_cat_name)
        msg.append("          - Loading Gaia source catalog: %s" % gaia_cat_name)
        msg.append("          - Position: (ra, dec) = (%.5f ; %+.5f)  within %.1f arcmin" % (hdr['CRVAL1'], hdr['CRVAL2'], radius))
    else:
        # Download Gaia positions:
        msg.append("          - Downloading Gaia source catalog... (%s)" % gaia_dr.upper())
        msg.append("          - Position: (ra, dec) = (%.5f ; %+.5f)  within %.1f arcmin" % (hdr['CRVAL1'], hdr['CRVAL2'], radius))
        try:
            ref_cat = get_gaia_catalog(hdr['CRVAL1'], hdr['CRVAL2'], radius=radius, catalog_fname=gaia_cat_name, database=gaia_dr)
            msg.append("          - Saving Gaia source catalog: %s" % gaia_cat_name)
        except:
            msg.append(" [ERROR]  - Could not reach Gaia server! Check your internet connection.")
            msg.append("")
            return "\n".join(msg)

    # reject brightest sources:
    bright_cut = ref_cat['phot_g_mean_mag'] > G_lim
    msg.append("          - Rejecting sources brighter than G < %.1f mag" % G_lim)

    # Get the source extraction catalog:
    sep_cat = Table.read(sep_fname)
    msg.append("          - Loaded image source catalog: %s" % sep_fname)
    ### Reject brightest and faintest 10%:
    # lower, upper = np.percentile(sep_cat['flux_auto'], [10, 90])
    # cut_flux = (sep_cat['flux_auto'] > lower) & (sep_cat['flux_auto'] < upper)
    # #sep_cat = sep_cat[cut_flux]

    # Pixel coordinates:
    sep_coords = np.array([sep_cat['x'], sep_cat['y']]).T

    # Convert to WCS:
    sep_wcs = pixel_to_radec(sep_coords, hdr)
    axis_ratio = sep_cat['b']/sep_cat['a']

    # Select only 'round' sources:
    axis_mask = axis_ratio > q_lim
    msg.append("          - Rejecting sources with axis ratio < %.2f" % q_lim)

    if np.sum(axis_mask) < min_num:
        msg.append(" [ERROR]  - Not enough targets found in the image!")
        msg.append(" [ERROR]  - Found %i but expected at least %i" % (np.sum(axis_mask), min_num))
        msg.append("")
        return "\n".join(msg)

    sep_wcs = sep_wcs[axis_mask]
    ref_coords = np.array([ref_cat['ra'], ref_cat['dec']]).T
    ref_coords = ref_coords[bright_cut]

    if len(ref_coords) < len(sep_wcs):
        matched_sep = match_catalogs(ref_coords, sep_wcs)
        matched_ref = ref_coords
    else:
        matched_ref = match_catalogs(sep_wcs, ref_coords)
        matched_sep = sep_wcs

    # Reject outliers:
    msg.append("          - Performing initial cross identification")
    dist = np.sqrt(np.sum((matched_ref - matched_sep)**2, axis=1))
    mask = np.abs(dist - np.median(dist)) < kappa*mad(dist)
    msg.append("          - Rejecting outliers: > %.1f MAD (median abs. deviation)" % kappa)

    matched_sep = matched_sep[mask]
    matched_ref = matched_ref[mask]


    # Fit the WCS transformation:
    _, crval, CD, _ = get_WCS(hdr)
    msg.append("          - Fitting WCS transformation using %i sources" % len(matched_ref))
    wcs_keys = update_WCS(matched_sep, matched_ref, crval, CD)
    hdr.update(wcs_keys)
    hdr.add_comment("PyNOT: WCS calibration using Gaia %s" % gaia_dr)
    hdr['RADESYSa'] = hdr['RADECSYS']
    if 'RADECSYS' in hdr:
        hdr.pop('RADECSYS')
    msg.append("          - Updating WCS information")


    # -- Plot solution:
    plt.close('all')
    warnings.simplefilter('ignore', category=AstropyWarning)
    wcs_new = WCS(hdr)
    fig = plt.figure()
    ax = fig.add_subplot(111, projection=wcs_new)
    med_val = np.median(img)
    ax.imshow(img, vmin=med_val-1*mad(img), vmax=med_val+10*mad(img),
              origin='lower', cmap=plt.cm.gray_r)
    ax.scatter(matched_ref[:, 0], matched_ref[:, 1],
               transform=ax.get_transform('fk5'), edgecolor='r', facecolor='none', s=50)
    ax.set_xlabel("Right Ascension")
    ax.set_ylabel("Declination")
    # ax.set_title("WCS precision: %.3f arcsec" % wcs_resid)
    fig.savefig(fig_fname)
    msg.append(" [OUTPUT] - Saving WCS solution figure: %s" % fig_fname)


    # -- Calculate Dispersion in WCS Solution:
    sep_wcs = pixel_to_radec(sep_coords, hdr)
    ref_coords = np.array([ref_cat['ra'], ref_cat['dec']]).T
    ref_coords = ref_coords[bright_cut]
    if len(ref_coords) < len(sep_wcs):
        matched_sep = match_catalogs(ref_coords, sep_wcs)
        matched_ref = ref_coords
    else:
        matched_ref = match_catalogs(sep_wcs, ref_coords)
        matched_sep = sep_wcs
    dist = np.sqrt(np.sum((matched_ref - matched_sep)**2, axis=1))
    mask = dist - np.median(dist) < kappa*mad(dist)
    wcs_resid = np.std(dist[mask]) * 3600.
    msg.append("          - WCS precision: %.3f arcsec" % wcs_resid)


    # Update photometric table:
    sep_cat['ra'] = sep_wcs[:, 0]
    sep_cat['dec'] = sep_wcs[:, 1]
    sep_cat.write(sep_fname, format='fits', overwrite=True)
    msg.append(" [OUTPUT] - Updating source identification table: %s" % sep_fname)


    # -- Update image:
    with fits.open(img_fname) as hdu_list:
        hdu_list['DATA'].header = hdr
        hdu_list['ERR'].header.update(wcs_keys)
        hdu_list.writeto(output_fname, overwrite=True)

    msg.append(" [OUTPUT] - Saving WCS calibrated image: %s" % output_fname)
    msg.append("")
    output_msg = "\n".join(msg)
    return output_msg

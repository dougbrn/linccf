from functools import lru_cache
from io import BytesIO

import astropy.units as u
import lsst.geom as geom
import matplotlib.pyplot as plt
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from astropy.coordinates import SkyCoord
from astropy.visualization import ZScaleInterval
from astropy.visualization.wcsaxes import SphericalCircle
from astropy.wcs import WCS
from ipywidgets import Layout, Output, VBox, HBox
from IPython.display import display
from lsdb import read_hats
from PIL import Image
from reproject import reproject_interp


BAND_COLORS = {'u': '#0c71ff', 'g': '#49be61', 'r': '#c61c00',
               'i': '#ffc200', 'z': '#f341a2', 'y': '#5d0000'}


def create_wcs(ra, dec, size=100, pixel_scale=0.2):
    w = WCS(naxis=2)
    w.wcs.crpix = [size / 2, size / 2]
    w.wcs.crval = [ra, dec]
    w.wcs.cdelt = [pixel_scale / 3600] * 2  # LSST pixel scale
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]  # Use TAN projection
    return w


def get_cutout(butler, data_id, ra, dec, size=100):
    wcs = butler.get('calexp.wcs', **data_id)
    detector_box = butler.get('calexp.detector', **data_id).getBBox()
    xy = geom.PointI(wcs.skyToPixel(geom.SpherePoint(ra, dec, geom.degrees)))

    cutout_size = geom.ExtentI(size, size)
    cutout_box = geom.BoxI(xy - cutout_size // 2, cutout_size)
    cutout_box = cutout_box.clippedTo(detector_box)

    return butler.get('calexp', **data_id, parameters={'bbox': cutout_box}), cutout_box


def plot_cutout(butler, visit_id, detector_id, ra, dec, size=100, reproject_wcs=None):
    data_id = dict(visit=visit_id, detector=detector_id, instrument="LSSTComCam")
    
    cutout, cutout_box = get_cutout(butler, data_id, ra, dec, size=size)
    cutout_box_min_corner = cutout_box.getCorners()[0]

    cutout10_array = get_cutout(butler, data_id, ra, dec, size=10)[0].getImage().getArray()
    ap10_flux = cutout10_array.sum()

    astropy_wcs = WCS(cutout.wcs.getFitsMetadata())
    astropy_wcs.wcs.crpix[0] -= cutout_box_min_corner.x
    astropy_wcs.wcs.crpix[1] -= cutout_box_min_corner.y

    image_array = cutout.getImage().getArray()
    # cutout.getMask()
    # cutout.getVariance()
    if reproject_wcs is not None:
        image_array, _footprint = reproject_interp(
            (image_array, astropy_wcs),
            reproject_wcs,
            shape_out=(size, size),
            order='bicubic',
        )
        astropy_wcs = reproject_wcs
    
    interval = ZScaleInterval()
    _vmin, vmax = interval.get_limits(cutout10_array)

    plt.figure(figsize=(8, 8), dpi=200)
    plt.subplot(projection=astropy_wcs)
    plt.title(f"visit: {visit_id}, detector: {detector_id}, {ra=:.5f}, {dec=:.5f}\n{ap10_flux=:.0f}")
    lon = plt.gca().coords[0]
    lat = plt.gca().coords[1]
    lon.set_major_formatter('dd:mm:ss')
    lat.set_major_formatter('dd:mm:ss')
    lon.set_ticklabel(exclude_overlapping=False)
    lat.set_ticklabel(exclude_overlapping=False)
    lon.set_ticklabel_position('b')
    lat.set_ticklabel_position('l')
    plt.imshow(image_array, cmap='gray', vmin=0, vmax=vmax, origin='lower')
    plt.xlabel('RA')
    plt.ylabel('Dec')
    # circle patch for source position
    circle = SphericalCircle(SkyCoord(ra, dec, unit=u.deg), 1*u.arcsec, edgecolor='red', facecolor='none', 
                             transform=plt.gca().get_transform('icrs'))
    plt.gca().add_patch(circle)
    lon.set_ticks(spacing=3. * u.arcsec)
    lat.set_ticks(spacing=3. * u.arcsec)
    plt.grid(color='white', alpha=0.5, ls='solid')
    plt.colorbar()

    buf = BytesIO()
    plt.savefig(buf, format='png', pad_inches=0)
    plt.close()

    buf.seek(0)
    image = Image.open(buf)
    return np.asarray(image)


@lru_cache(maxsize=1024)
def load_object_and_forced(oid, hats_path, filter=True):
    filters = [("objectId", "==", oid)]

    comcam_obj = hats_path / "object"
    comcam_src = hats_path / "forcedSource"

    obj = read_hats(
        comcam_obj,
        columns=["objectId", "coord_ra", "coord_dec"],
        filters=filters,
    )
    src_flat = read_hats(comcam_src, filters=filters)
    src_nested = obj.join_nested(
        src_flat,
        nested_column_name="lc",
        left_on="objectId",
        right_on="objectId",
    )

    ndf = src_nested.compute()
    if filter:
        ndf = ndf.query(
            "~lc.psfFlux_flag"
            " and ~lc.pixelFlags_suspect"
            " and ~lc.pixelFlags_saturated"
            " and ~lc.pixelFlags_cr"
            " and ~lc.pixelFlags_bad"
        )
    return ndf.iloc[0]


def make_figure(oid, butler, hats_path, image_size=100):
    data = load_object_and_forced(oid, hats_path)
    lc = data.lc.sort_values("midpointMjdTai").reset_index()

    wcs = create_wcs(data.coord_ra, data.coord_dec, size=image_size)

    # Text about selection
    out_text = Output()
    
    @lru_cache(maxsize=4098)
    def get_image_by_idx(idx):
        row = lc.iloc[idx]
        image = plot_cutout(
            butler,
            visit_id=row.visit,
            detector_id=row.detector,
            ra=data.coord_ra,
            dec=data.coord_dec,
            size=image_size,
            reproject_wcs=wcs,
        )
        return image

    def update_text_by_idx(idx):
        row = lc.iloc[idx]
        out_text.clear_output()
        with out_text:
            display(row)

    update_text_by_idx(0)

    # Light curve plot
    y_min, y_max = lc.psfMag.min(), lc.psfMag.max()
    y_ampl = y_max - y_min
    range_y = [y_max + 0.1 * y_ampl, y_min - 0.1 * y_ampl]
    colors = lc["band"].map(BAND_COLORS)
    scatter_trace = go.Scatter(
        x=lc["midpointMjdTai"],
        y=lc["psfMag"],
        error_y=dict(type="data", array=lc["psfMagErr"], visible=True),
        mode="markers",
        marker=dict(size=10, color=colors),  # Use color mapping
        name="Light Curve"
    )
    lc_fig = go.FigureWidget(data=[scatter_trace])
    lc_fig.update_layout(
        title=f"{data.objectId}",
        xaxis_title="midpointMjdTai",
        yaxis_title="psfMag",
        yaxis=dict(range=range_y),
        width=800,
        height=600,
    )
    out_lc_fig = Output()
    with out_lc_fig:
        display(lc_fig)

    # Cutout
    image = get_image_by_idx(0)
    cutout_fig = go.FigureWidget(
        data=[go.Image(z=image)],
        layout=dict(
            width=800,
            height=800,
            xaxis=dict(
                visible=False,  # Hide x-axis
                showgrid=False,  # Remove grid
                zeroline=False,
                showticklabels=False
            ),
            yaxis=dict(
                visible=False,  # Hide y-axis
                showgrid=False,  # Remove grid
                zeroline=False,
                showticklabels=False
            ),
            margin=dict(l=0, r=0, t=0, b=0)  # Remove any margins
        )
    )
    out_cutout_fig = Output()
    with out_cutout_fig:
        display(cutout_fig)

    # Callback function to update the image on click
    def update_image(trace, points, selector):
        print(points)
        
        if not points.point_inds:
            return
        idx = points.point_inds[0]
        
        update_text_by_idx(idx)
        
        image = get_image_by_idx(idx)
        cutout_fig.data[0].z = image

    lc_fig.data[0].on_click(update_image)
    
    return HBox([out_lc_fig, out_cutout_fig, out_text])
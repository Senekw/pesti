"""Render the Phase 0 checkpoint grids.

Produces the two cases the checkpoint asks for - a 1 km square and an irregular polygon -
plus the exclusions case, because that is where the geometry pipeline is most likely to be
wrong in a way a summary statistic would hide.

Encoding notes, since these are maps and it is easy to make them merely colourful:

* Block fill is a **sequential** single-hue ramp on plantable fraction. That is a magnitude,
  so it gets one hue light-to-dark rather than a categorical set. Crop assignment does not
  exist until Phase 2; the honest thing to show now is how much of each block survived
  headlands and exclusions.
* Headland and exclusions are neutral grey separated by **hatch angle**, not by hue. They are
  masks, not data categories, and giving them their own hues would imply they belong to the
  same scale as the blocks.
* Every non-obvious region is directly labelled, so nothing depends on colour alone.

Run: python scripts/render_grid_examples.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import matplotlib

matplotlib.use("Agg")

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, Normalize
from shapely.geometry import LineString, Polygon

from intercrop.domain._types import as_shapely
from intercrop.domain.field import Exclusion, ExclusionKind, Field_
from intercrop.domain.grid import GridSpec
from intercrop.geometry import WGS84_EPSG, reproject, utm_epsg_for
from intercrop.grid.generator import build_grid, grid_summary, synthesise_rectangle

OUT = Path(__file__).resolve().parents[1] / "out"
PUNJAB_LON, PUNJAB_LAT = 75.85, 30.90

# Sequential blue ramp, steps 100 -> 700 from the reference palette.
BLUE_RAMP = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
    "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
]
SEQUENTIAL = LinearSegmentedColormap.from_list("plantable", BLUE_RAMP)

INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRIDLINE = "#e1e0d9"
SURFACE = "#fcfcfb"


def _style_axes(ax: plt.Axes, title: str, subtitle: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_aspect("equal")
    ax.set_title(title, color=INK, fontsize=13, fontweight="bold", loc="left", pad=30)
    ax.text(
        0.0, 1.008, subtitle, transform=ax.transAxes, color=INK_SECONDARY, fontsize=9.0,
        va="bottom", ha="left",
    )
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8.5, length=0)
    ax.grid(True, color=GRIDLINE, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    ax.set_xlabel("metres east of field origin", color=MUTED, fontsize=8.5)
    ax.set_ylabel("metres north of field origin", color=MUTED, fontsize=8.5)


def _plot_polygon(ax: plt.Axes, geom, ox: float, oy: float, **kwargs) -> None:
    """Draw a shapely polygon (or multipolygon) with coordinates relative to an origin."""
    parts = geom.geoms if hasattr(geom, "geoms") else [geom]
    for part in parts:
        if part.is_empty or not hasattr(part, "exterior"):
            continue
        xs, ys = part.exterior.xy
        ax.fill(
            [x - ox for x in xs], [y - oy for y in ys], **kwargs
        )


def render(field: Field_, spec: GridSpec, path: Path, title: str) -> dict[str, object]:
    epsg = field.working_crs_epsg
    grid = build_grid(field, spec)
    summary = grid_summary(field, grid)

    boundary = field.boundary_m()
    ox, oy, _, _ = boundary.bounds

    fig, ax = plt.subplots(figsize=(9.2, 8.4), dpi=140)
    fig.patch.set_facecolor(SURFACE)

    subtitle = (
        f"{summary['block_count']} blocks at {summary['block_size_m']:g} m "
        f"({summary['passes_per_block']} passes x {spec.implement_width_m:g} m implement) "
        f"| {summary['plantable_area_ha']:.1f} ha plantable of "
        f"{summary['gross_area_ha']:.1f} ha gross | grid azimuth "
        f"{summary['azimuth_deg']:g} deg ({summary['azimuth_source'].replace('_', ' ')})"
    )
    _style_axes(ax, title, subtitle)

    # Headland: the band between the boundary and the eroded plantable region.
    headland = boundary.difference(boundary.buffer(-spec.headland_depth_m))
    _plot_polygon(
        ax, headland, ox, oy, facecolor="#e1e0d9", edgecolor=MUTED, linewidth=0.5,
        hatch="///", zorder=1,
    )

    # Blocks, shaded by how much of the nominal cell survived.
    norm = Normalize(vmin=0.0, vmax=1.0)
    for block in grid.blocks:
        _plot_polygon(
            ax,
            block.geometry_m(epsg),
            ox,
            oy,
            facecolor=SEQUENTIAL(norm(block.plantable_fraction)),
            edgecolor=SURFACE,
            linewidth=0.45,  # a surface-coloured gap between adjacent fills
            zorder=2,
        )

    # Exclusions on top, hatched the other way so they never read as blocks.
    footprint = field.exclusion_footprint_m()
    if footprint is not None:
        _plot_polygon(
            ax, footprint.intersection(boundary), ox, oy, facecolor="#c3c2b7",
            edgecolor=INK_SECONDARY, linewidth=0.7, hatch="\\\\\\", zorder=3,
        )
        for exclusion in field.exclusions:
            shape = exclusion.footprint(epsg).intersection(boundary)
            if shape.is_empty:
                continue
            # Label a point along the feature rather than its centroid. Two crossing linear
            # exclusions both have their centroid at the crossing, so centroid labels
            # collide and one hides the other.
            source = as_shapely(exclusion.geometry)
            if source.geom_type in ("LineString", "MultiLineString"):
                line = reproject(source, WGS84_EPSG, epsg)
                anchor = line.interpolate(0.22, normalized=True)
            else:
                anchor = shape.representative_point()
            ax.annotate(
                exclusion.label or str(exclusion.kind),
                xy=(anchor.x - ox, anchor.y - oy),
                color=INK,
                fontsize=8,
                fontweight="bold",
                ha="center",
                va="center",
                zorder=5,
                bbox={"boxstyle": "round,pad=0.28", "facecolor": SURFACE,
                      "edgecolor": MUTED, "linewidth": 0.5},
            )

    # Field boundary outline last, so it reads as the containing edge.
    bxs, bys = boundary.exterior.xy
    ax.plot(
        [x - ox for x in bxs], [y - oy for y in bys], color=INK, linewidth=1.6, zorder=4
    )

    colourbar = fig.colorbar(
        plt.cm.ScalarMappable(norm=norm, cmap=SEQUENTIAL), ax=ax, fraction=0.038, pad=0.03
    )
    colourbar.set_label("plantable fraction of block", color=INK_SECONDARY, fontsize=9)
    colourbar.ax.tick_params(colors=MUTED, labelsize=8, length=0)
    colourbar.outline.set_visible(False)

    partial = summary["partial_block_count"]
    ax.legend(
        handles=[
            mpatches.Patch(
                facecolor="#e1e0d9", edgecolor=MUTED, hatch="///",
                # Not labelled with a hectare figure: headland and exclusions overlap where
                # a road runs along the boundary, so the two areas do not sum and quoting
                # them separately would imply they do. The subtitle carries the honest
                # plantable-versus-gross total.
                label=f"headland, {spec.headland_depth_m:g} m deep "
                      f"(2 x {spec.implement_width_m:g} m implement)",
            ),
            mpatches.Patch(
                facecolor="#c3c2b7", edgecolor=INK_SECONDARY, hatch="\\\\\\",
                label=f"exclusions ({summary['exclusion_area_ha']:.2f} ha)",
            ),
            mpatches.Patch(
                facecolor=SEQUENTIAL(0.55), edgecolor=SURFACE,
                label=f"{partial} partial blocks, {grid.block_count - partial} full",
            ),
        ],
        loc="upper left",
        bbox_to_anchor=(0.0, -0.075),
        ncol=3,
        frameon=False,
        fontsize=8.5,
        labelcolor=INK_SECONDARY,
    )

    note = summary["snap_note"]
    if note:
        fig.text(0.5, -0.005, str(note), ha="center", va="top", color=MUTED, fontsize=8,
                 wrap=True)

    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return summary


def square_km_field() -> Field_:
    return Field_.from_boundary(
        "Punjab 1 km square",
        synthesise_rectangle(PUNJAB_LON, PUNJAB_LAT, 1000.0, 1000.0),
        region_code="IN-PB",
        boundary_source="grower_stated_dimensions",
    )


def irregular_field() -> Field_:
    from shapely.geometry import Point

    epsg = utm_epsg_for(PUNJAB_LON, PUNJAB_LAT)
    origin = reproject(Point(PUNJAB_LON, PUNJAB_LAT), WGS84_EPSG, epsg)
    ox, oy = origin.x, origin.y
    vertices = [
        (0.0, 0.0), (1200.0, 700.0), (1500.0, 1100.0), (1100.0, 1400.0),
        (700.0, 1000.0), (500.0, 1150.0), (150.0, 600.0),
    ]
    polygon = Polygon([(ox + x, oy + y) for x, y in vertices])
    return Field_.from_boundary(
        "Irregular parcel",
        dict(reproject(polygon, epsg, WGS84_EPSG).__geo_interface__),
        region_code="IN-PB",
        boundary_source="uploaded_geojson",
    )


def field_with_exclusions() -> Field_:
    field = square_km_field()
    epsg = field.working_crs_epsg
    min_x, min_y, max_x, max_y = field.boundary_m().bounds
    mid_x, mid_y = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0

    def to_wgs84(geom) -> dict:
        return dict(reproject(geom, epsg, WGS84_EPSG).__geo_interface__)

    return field.model_copy(
        update={
            "exclusions": (
                Exclusion(
                    kind=ExclusionKind.ROAD,
                    geometry=to_wgs84(LineString([(min_x, mid_y), (max_x, mid_y)])),
                    width_m=6.0,
                    label="farm track (6 m)",
                ),
                Exclusion(
                    kind=ExclusionKind.IRRIGATION_MAIN,
                    geometry=to_wgs84(LineString([(mid_x, min_y), (mid_x, max_y)])),
                    width_m=2.0,
                    standoff_m=1.5,
                    label="irrigation main",
                ),
                Exclusion(
                    kind=ExclusionKind.BUILDING,
                    geometry=to_wgs84(
                        Polygon([
                            (min_x + 60, min_y + 60), (min_x + 160, min_y + 60),
                            (min_x + 160, min_y + 140), (min_x + 60, min_y + 140),
                        ])
                    ),
                    label="pump shed",
                ),
                Exclusion(
                    kind=ExclusionKind.DRAINAGE,
                    geometry=to_wgs84(
                        LineString([(min_x, min_y + 780), (max_x * 0.4 + min_x * 0.6, max_y)])
                    ),
                    width_m=4.0,
                    label="drain",
                ),
            )
        }
    )


def main() -> None:
    OUT.mkdir(exist_ok=True)
    spec = GridSpec(requested_block_size_m=50.0, implement_width_m=1.8)

    cases = [
        (square_km_field(), OUT / "grid_square_km.png",
         "1 km x 1 km cold-start square, Punjab"),
        (irregular_field(), OUT / "grid_irregular.png",
         "Irregular parcel with a concave notch"),
        (field_with_exclusions(), OUT / "grid_with_exclusions.png",
         "1 km square with road, irrigation main, drain and building"),
    ]

    for field, path, title in cases:
        summary = render(field, spec, path, title)
        print(f"\n{title}\n  -> {path.name}")
        for key in (
            "gross_area_ha", "plantable_area_ha", "lost_to_headland_and_exclusion_ha",
            "exclusion_area_ha", "block_count", "partial_block_count",
            "dropped_sliver_count", "block_size_m", "azimuth_deg", "azimuth_source",
        ):
            print(f"     {key:38s} {summary[key]}")
        print(f"     {'grid_content_hash':38s} {str(summary['grid_content_hash'])[:16]}...")


if __name__ == "__main__":
    main()


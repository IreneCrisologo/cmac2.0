""" Code that calculates clutter by using running stats. """

from copy import deepcopy

import dask.array as da
from dask import delayed
import numpy as np
import pyart


def tall_clutter(files, clutter_thresh_min=0.0002,
                 clutter_thresh_max=1.5, radius=1,
                 write_radar=True, out_file=None,
                 use_dask=False):
    """
    Wind Farm Clutter Calculation

    Parameters
    ----------
    files : list
        List of radar files used for the clutter calculation.

    Other Parameters
    ----------------
    clutter_thresh_min : float
        Threshold value for which, any clutter values above the
        clutter_thres_min will be considered clutter, as long as they
        are also below the clutter_thres_max.
    clutter_thresh_max : float
        Threshold value for which, any clutter values below the
        clutter_thres_max will be considered clutter, as long as they
        are also above the clutter_thres_min.
    radius : int
        Radius of the area surrounding the clutter gate that will
        be also flagged as clutter.
    write_radar : bool
        Whether to or not, to write the clutter radar as a netCDF file.
        Default is True.
    out_file : string
        String of location and filename to write the radar object too,
        if write_radar is True.
    use_dask : bool
        Use dask instead of running stats for calculation. The will reduce
        run time.

    Returns
    -------
    clutter_radar : Radar
        Radar object with the clutter field that was calculated.
        This radar only has the clutter field, but maintains all
        other radar specifications.

    """


    def get_reflect_array(file, first_shape):
        """ Retrieves a reflectivity array for a radar volume. """
        try:
            radar = pyart.io.read(file)
            reflect_array = deepcopy(radar.fields['reflectivity']['data'])
            del radar

            if reflect_array.shape == first_shape:
                return reflect_array.filled(fill_value=np.nan)
        except TypeError:
            print(file + ' is corrupt...skipping!')
        return np.nan*np.zeros(first_shape)

    if use_dask is False:
        run_stats = _RunningStats()
        first_shape = 0
        for file in files:
            try:
                radar = pyart.io.read(file)
                reflect_array = radar.fields['reflectivity']['data']
                if first_shape == 0:
                    first_shape = reflect_array.shape
                    clutter_radar = radar
                    run_stats.push(reflect_array)
                if reflect_array.shape == first_shape:
                    run_stats.push(reflect_array)
                del radar
            except TypeError:
                print(file + ' is corrupt...skipping!')
                continue
        mean = run_stats.mean()
        stdev = run_stats.standard_deviation()
        clutter_values = stdev / mean
    else:
        first_shape = 0
        i = 0
        while first_shape == 0:
            try:
                radar = pyart.io.read(files[i])
                reflect_array = radar.fields['reflectivity']['data']
                first_shape = reflect_array.shape
                clutter_radar = radar
            except TypeError:
                i = i + 1
                print(file + ' is corrupt...skipping!')
                continue
        arrays = [delayed(get_reflect_array)(file, first_shape)
                  for file in files]
        array = [da.from_delayed(a, shape=first_shape, dtype=float)
                 for a in arrays]
        array = da.stack(array, axis=0)
        print('## Calculating mean in parallel...')
        mean = np.array(da.nanmean(array, axis=0))
        print('## Caluclating standard deviation...')
        stdev = np.array(da.nanstd(array, axis=0))
        clutter_values = stdev / mean
        clutter_values = np.ma.masked_invalid(clutter_values)

    shape = clutter_values.shape
    mask = np.ma.getmask(clutter_values)
    is_clutters = np.argwhere(
        np.logical_and(clutter_values > clutter_thresh_min,
                       clutter_values < clutter_thresh_max))
    clutter_array = _clutter_marker(is_clutters, shape, mask, radius)
    clutter_radar.fields.clear()
    clutter_dict = _clutter_to_dict(clutter_array)
    clutter_radar.add_field('xsapr_clutter', clutter_dict,
                            replace_existing=True)
    if write_radar is True:
        pyart.io.write_cfradial(out_file, clutter_radar)
    del clutter_radar
    return


# Adapted from http://stackoverflow.com/a/17637351/6392167
class _RunningStats():
    """ Calculated Mean, Variance and Standard Deviation, but
    uses the Welford algorithm to save memory. """

    def __init__(self):
        self.n = 0
        self.old_m = 0
        self.new_m = 0
        self.old_s = 0
        self.new_s = 0

    def clear(self):
        """ Clears n variable in stat calculation. """
        self.n = 0

    def push(self, x):
        """ Takes an array and the previous array and calculates mean,
        variance and standard deviation, and continues to take multiple
        arrays one at a time. """
        shape = x.shape
        ones_arr = np.ones(shape)
        mask = np.ma.getmask(x)
        mask_ones = np.ma.array(ones_arr, mask=mask)
        add_arr = np.ma.filled(mask_ones, fill_value=0.0)
        self.n += add_arr
        mask_n = np.ma.array(self.n, mask=mask)
        fill_n = np.ma.filled(mask_n, fill_value=1.0)

        if self.n.max() == 1.0:
            self.old_m = self.new_m = np.ma.filled(x, 0.0)
            self.old_s = np.zeros(shape)
        else:
            self.new_m = np.nansum(np.dstack(
                (self.old_m, (x-self.old_m) / fill_n)), 2)
            self.new_s = np.nansum(np.dstack(
                (self.old_s, (x-self.old_m) * (x-self.new_m))), 2)

            self.old_m = self.new_m
            self.old_s = self.new_s

    def mean(self):
        """ Returns mean once all arrays are inputed. """
        return self.new_m if np.any(self.n) else 0.0

    def variance(self):
        """ Returns variance once all arrays are inputed. """
        return self.new_s / (self.n-1) if (self.n.max() > 1.0) else 0.0

    def standard_deviation(self):
        """ Returns standard deviation once all arrays are inputed. """
        return np.ma.sqrt(self.variance())


def _clutter_marker(is_clutters, shape, mask, radius):
    """ Takes clutter_values(stdev/mean)and the clutter_threshold
    and calculates where X-SAPR wind farm clutter is occurring at
    the SGP ARM site. """
    temp_array = np.zeros(shape)
    # Inserting here possible other fields that can help distinguish
    # whether a gate is clutter or not.
    temp_array = np.pad(temp_array, radius,
                        mode='constant', constant_values=-999)
    is_clutters = is_clutters + radius
    x_val, y_val = np.ogrid[-radius:(radius + 1),
                            -radius:(radius + 1)]
    circle = (x_val*x_val) + (y_val*y_val) <= (radius*radius)
    for is_clutter in is_clutters:
        ray, gate = is_clutter[0], is_clutter[1]
        frame = temp_array[ray - radius:ray + radius + 1,
                           gate - radius:gate + radius + 1]
        temp_array[ray - radius:ray + radius + 1,
                   gate - radius:gate + radius + 1] = np.logical_or(
                       frame, circle)
    temp_array = temp_array[radius:shape[0] + radius,
                            radius:shape[1] + radius]
    clutter_array = np.ma.array(temp_array, mask=mask)
    return clutter_array


def _clutter_to_dict(clutter_array):
    """ Function that takes the clutter array
    and turn it into a dictionary to be used and added
    to the pyart radar object. """
    clutter_dict = {}
    clutter_dict['units'] = 'unitless'
    clutter_dict['data'] = clutter_array
    clutter_dict['standard_name'] = 'xsapr_clutter'
    clutter_dict['long_name'] = 'X-SAPR Clutter'
    clutter_dict['notes'] = '0: No Clutter, 1: Clutter'
    return clutter_dict

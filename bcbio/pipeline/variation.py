"""Next-gen variant detection and evaluation with GATK and SnpEff.
"""
import os
import toolz as tz

from bcbio import utils
from bcbio.cwl import cwlutils
from bcbio.log import logger
from bcbio.pipeline import datadict as dd
from bcbio.variation.genotype import variant_filtration, get_variantcaller
from bcbio.variation import annotation, damage, effects, genotype, germline, prioritize, validate, vcfutils
from bcbio.variation import multi as vmulti

# ## CWL summarization

def summarize_vc(items):
    """CWL target: summarize variant calls and validation for multiple samples.
    """
    items = [utils.to_single_data(x) for x in validate.summarize_grading(items)]
    out = {"validate": _combine_validations(items),
           "variants": {"calls": [], "gvcf": []}}
    added = set([])
    for data in items:
        if data.get("vrn_file"):
            names = dd.get_batches(data)
            if not names:
                names = [dd.get_sample_name(data)]
            batch_name = names[0]
            if data.get("vrn_file_joint") is not None:
                to_add = [("vrn_file", "gvcf", dd.get_sample_name(data)),
                          ("vrn_file_joint", "calls", batch_name)]
            else:
                to_add = [("vrn_file", "calls", batch_name)]
            for vrn_key, out_key, name in to_add:
                cur_name = "%s-%s" % (name, dd.get_variantcaller(data))
                if cur_name not in added:
                    out_file = os.path.join(utils.safe_makedir(os.path.join(dd.get_work_dir(data),
                                                                            "variants", out_key)),
                                            "%s.vcf.gz" % cur_name)
                    added.add(cur_name)
                    # Ideally could symlink here but doesn't appear to work with
                    # Docker container runs on Toil where PATHs don't get remapped
                    utils.copy_plus(os.path.realpath(data[vrn_key]), out_file)
                    vcfutils.bgzip_and_index(out_file, data["config"])
                    out["variants"][out_key].append(out_file)
    return [out]

def _combine_validations(items):
    """Combine multiple batch validations into validation outputs.
    """
    csvs = set([])
    pngs = set([])
    for v in [x.get("validate") for x in items]:
        if v and v.get("grading_summary"):
            csvs.add(v.get("grading_summary"))
        if v and v.get("grading_plots"):
            pngs |= set(v.get("grading_plots"))
    if len(csvs) == 1:
        grading_summary = csvs.pop()
    else:
        grading_summary = os.path.join(utils.safe_makedir(os.path.join(dd.get_work_dir(items[0]), "validation")),
                                       "grading-summary-combined.csv")
        with open(grading_summary, "w") as out_handle:
            for i, csv in enumerate(sorted(list(csvs))):
                with open(csv) as in_handle:
                    h = in_handle.readline()
                    if i == 0:
                        out_handle.write(h)
                    for l in in_handle:
                        out_handle.write(l)
    return {"grading_plots": sorted(list(pngs)), "grading_summary": grading_summary}

# ## Genotyping

def postprocess_variants(items):
    """Provide post-processing of variant calls: filtering and effects annotation.
    """
    vrn_key = "vrn_file"
    if not isinstance(items, dict):
        items = [utils.to_single_data(x) for x in items]
        if "vrn_file_joint" in items[0]:
            vrn_key = "vrn_file_joint"
    data, items = _get_batch_representative(items, vrn_key)
    items = cwlutils.unpack_tarballs(items, data)
    data = cwlutils.unpack_tarballs(data, data)
    cur_name = "%s, %s" % (dd.get_sample_name(data), get_variantcaller(data))
    logger.info("Finalizing variant calls: %s" % cur_name)
    orig_vrn_file = data.get(vrn_key)
    data = _symlink_to_workdir(data, [vrn_key])
    data = _symlink_to_workdir(data, ["config", "algorithm", "variant_regions"])
    if data.get(vrn_key):
        logger.info("Calculating variation effects for %s" % cur_name)
        ann_vrn_file, vrn_stats = effects.add_to_vcf(data[vrn_key], data)
        if ann_vrn_file:
            data[vrn_key] = ann_vrn_file
        if vrn_stats:
            data["vrn_stats"] = vrn_stats
        orig_items = _get_orig_items(items)
        logger.info("Annotate VCF file: %s" % cur_name)
        data[vrn_key] = annotation.finalize_vcf(data[vrn_key], get_variantcaller(data), orig_items)
        logger.info("Filtering for %s" % cur_name)
        data[vrn_key] = variant_filtration(data[vrn_key], dd.get_ref_file(data),
                                              tz.get_in(("genome_resources", "variation"), data, {}),
                                              data, orig_items)
        logger.info("Prioritization for %s" % cur_name)
        prio_vrn_file = prioritize.handle_vcf_calls(data[vrn_key], data, orig_items)
        if prio_vrn_file != data[vrn_key]:
            data[vrn_key] = prio_vrn_file
            logger.info("Germline extraction for %s" % cur_name)
            data = germline.extract(data, orig_items)

        if dd.get_align_bam(data):
            data = damage.run_filter(data[vrn_key], dd.get_align_bam(data), dd.get_ref_file(data),
                                     data, orig_items)
    if orig_vrn_file and os.path.samefile(data[vrn_key], orig_vrn_file):
        data[vrn_key] = orig_vrn_file
    return [[data]]

def _get_orig_items(data):
    """Retrieve original items in a batch, handling CWL and standard cases.
    """
    if isinstance(data, dict):
        if dd.get_align_bam(data) and tz.get_in(["metadata", "batch"], data):
            return vmulti.get_orig_items(data)
        else:
            return [data]
    else:
        return data

def _symlink_to_workdir(data, key):
    """For CWL support, symlink files into a working directory if in read-only imports.
    """
    orig_file = tz.get_in(key, data)
    if orig_file and not orig_file.startswith(dd.get_work_dir(data)):
        variantcaller = genotype.get_variantcaller(data)
        if not variantcaller:
            variantcaller = "precalled"
        out_file = os.path.join(dd.get_work_dir(data), variantcaller, os.path.basename(orig_file))
        utils.safe_makedir(os.path.dirname(out_file))
        utils.symlink_plus(orig_file, out_file)
        data = tz.update_in(data, key, lambda x: out_file)
    return data

def _get_batch_representative(items, key):
    """Retrieve a representative data item from a batch.

    Handles standard bcbio cases (a single data item) and CWL cases with
    batches that have a consistent variant file.
    """
    if isinstance(items, dict):
        return items, items
    else:
        vals = set([])
        out = []
        for data in items:
            if key in data:
                vals.add(data[key])
                out.append(data)
        if len(vals) != 1:
            raise ValueError("Incorrect values for %s: %s" % (key, list(vals)))
        return out[0], items

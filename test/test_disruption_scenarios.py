from pathlib import Path
import pytest

from scripts.disruption_scenarios import select_disruption
from scripts.run_disruption_campaign import VARIANTS, YEARS, configfiles, load_config, campaign_tasks, check_inputs, task_targets


def test_complete_matrix_and_unique_new_output_paths():
    targets = []
    counts = {}
    for campaign, expected in [('baseline', 15), ('interconnector', 525), ('fuel', 675)]:
        tasks = []
        for variant in VARIANTS:
            config = load_config(configfiles(variant, campaign))
            tasks.extend(campaign_tasks(config, YEARS, variant=variant))
        assert len(tasks) == expected
        counts[campaign] = len(tasks)
        targets.extend(task['target'] for task in tasks)
        assert all(t['target'].startswith('results/disruption_campaign_v2/') for t in tasks)
        if campaign == 'fuel':
            for task in tasks:
                event = task['definition']['fuel_link_disruptions'][0]
                assert event['locations'] == ['DK0 0AC']
                assert event['reduction'] in [.33, .66, 1.0]
        if campaign == 'interconnector':
            assert len({tuple(t['definition']['interconnector_outages'][0].get('lines', [])) + tuple(t['definition']['interconnector_outages'][0].get('links', [])) for t in tasks}) == 7
    assert len(targets) == len(set(targets)) == 1215


def test_baseline_is_shared_between_campaigns_and_weather_changes_no_reference():
    selections = []
    for campaign in ['baseline', 'interconnector', 'fuel']:
        config = load_config(configfiles('2035_w_V2G', campaign))
        selection = select_disruption({**config, 'disruption_variant':'2035_w_V2G', 'disruption_weather_year':'1965-1966'})
        selections.append(selection)
    for key in ['base_network', 'household_shares', 'weather_network', 'baseline_directory', 'rolling_horizon', 'load_shedding']:
        assert all(s[key] == selections[0][key] for s in selections)
    assert selections[0]['weather_network'] == 'resources/weather_1965_66/sector_2035_w_V2G/networks/base_s_10___2035.nc'
    fuel = selections[0]['load_shedding']['fuel']
    assert all(fuel['costs_by_fuel'][f] == 23300 for f in ['oil','gas','biomass'])
    assert fuel['costs_by_fuel']['hydrogen'] == fuel['hydrogen_balance']['cost'] == 200000


def test_input_check_requires_completed_baseline(tmp_path):
    config = load_config(configfiles('2025', 'fuel'))
    tasks = campaign_tasks(config, ['2013-2014'])
    with pytest.raises(FileNotFoundError, match='event_windows.csv'):
        check_inputs(tasks)
    assert any(path.endswith('applied_disruption.csv') for path in task_targets(tasks[0]))
    with pytest.raises(ValueError, match='unique'):
        campaign_tasks(config, ['2013-2014', '2013-2014'])
    with pytest.raises(ValueError, match='Unknown'):
        select_disruption({**config, 'disruption_weather_year':'bad'})


def test_sophia_launchers_have_matching_task_ranges():
    root = Path(__file__).resolve().parents[1]
    baseline = (root/'run_disruption_baselines.sbatch').read_text()
    assert '#SBATCH --array=0-14%4' in baseline
    for variant in VARIANTS:
        for campaign, last in [('interconnector',174),('fuel',224)]:
            text = (root/f'run_{campaign}_{variant}.sbatch').read_text()
            assert f'#SBATCH --array=0-{last}%4' in text
            assert f'--campaign {campaign} --variant {variant}' in text
            assert '--rerun-triggers mtime' not in text


def test_stale_baseline_and_changed_policy_are_rejected(tmp_path):
    import csv
    import json
    from scripts.disruption_scenarios import common_policy

    config = load_config(configfiles('2025', 'fuel'))
    task = campaign_tasks(config, ['2013-2014'])[0]
    settings = task['settings']
    reference = tmp_path / 'reference.nc'
    reference.write_text('fixture')
    settings.update(base_network=str(reference), household_shares=None, weather_network=None,
                    baseline_directory=str(tmp_path / 'baseline'))
    config['disruption']['variants']['2025'].update(base_network=str(reference), household_shares=None)
    overlay = tmp_path / 'config.yaml'
    import yaml
    overlay.write_text(yaml.safe_dump(config))
    task['configfiles'] = [str(overlay)]
    directory = Path(settings['baseline_directory'])
    directory.mkdir()
    (directory / 'network_prepared.nc').write_text('fixture')
    result = directory / 'base_weather'
    result.mkdir()
    (result / 'run_info.json').write_text(json.dumps({'common_policy': common_policy(settings, config['solving'], config)}))
    (result / 'status.json').write_text(json.dumps({'phase': 'complete'}))
    (result / 'network_solved.nc').write_text('fixture')
    timing = directory / 'event_windows.csv'
    with timing.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['variant','weather_year','duration_hours'])
        writer.writeheader()
        writer.writerows({'variant':'2025','weather_year':'2013-2014','duration_hours':h} for h in settings['durations_hours'])
    check_inputs([task])
    settings['load_shedding']['elec_cost'] += 1
    with pytest.raises(ValueError, match='settings changed'):
        check_inputs([task])
    settings['load_shedding']['elec_cost'] -= 1
    reference.write_text('changed input')
    with pytest.raises(ValueError, match='inputs/code changed'):
        check_inputs([task])

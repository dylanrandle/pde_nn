import numpy as np
import torch
import torch.nn as nn
import os
import time

from denn.utils import plot_results, plot_multihead, calc_gradient_penalty, handle_overwrite, plot_3D
from denn.config.config import write_config

try:
    from ray import tune
except:
    print("Ray not loaded.")

this_dir = os.path.dirname(os.path.abspath(__file__)) 

def train_GAN(G, D, problem, method='unsupervised', niters=100,
    g_lr=1e-3, g_betas=(0.0, 0.9), d_lr=1e-3, d_betas=(0.0, 0.9),
    lr_schedule=True, gamma=0.999, noise=False, step_size=15, obs_every=1, 
    d1=1., d2=1., G_iters=1, D_iters=1, wgan=True, gp=0.1, conditional=True, 
    train_mse=True, log=True, plot=True, multihead=False, plot_sep_curves=False, 
    save=False, save_G=False, dirname='train_GAN', config=None, 
    save_for_animation=False, **kwargs):
    """
    Train/test GAN method: supervised/semisupervised/unsupervised
    """
    assert method in ['supervised', 'semisupervised', 'unsupervised'], f'Method {method} not understood!'

    pkey = dirname.split('_')[0].lower()
    dirname = os.path.join(this_dir, '../experiments/runs', dirname)
    if plot and save:
        handle_overwrite(dirname)

    # validation: fixed grid/solution
    grid = problem.get_grid()
    soln = problem.get_solution(grid)

    # initialize residuals and grid for active sampling
    grid_samp = grid
    residuals = torch.zeros_like(grid_samp)
    residuals_delta = torch.zeros_like(grid_samp)

    # # observer mask and masked grid/solution (t_obs/y_obs)
    observers = torch.arange(0, len(grid), obs_every)
    # grid_obs = grid[observers, :]
    # soln_obs = soln[observers, :]

    # labels
    real_label = 1
    fake_label = -1 if wgan else 0
    real_labels = torch.full((len(grid),), real_label, dtype=torch.float).reshape(-1,1)
    fake_labels = torch.full((len(grid),), fake_label, dtype=torch.float).reshape(-1,1)
    # masked label vectors
    real_labels_obs = real_labels[observers, :]
    fake_labels_obs = fake_labels[observers, :]

    # initialize difference parameter for noise
    if noise:
        diff = 0

    # optimization
    optiG = torch.optim.Adam(G.parameters(), lr=g_lr, betas=g_betas)
    optiD = torch.optim.Adam(D.parameters(), lr=d_lr, betas=d_betas)
    #optiG = torch.optim.SGD(G.parameters(), lr=g_lr, momentum=g_momentum, nesterov=True)
    #optiD = torch.optim.SGD(D.parameters(), lr=d_lr, momentum=d_momentum, nesterov=True)
    if lr_schedule:
        lr_scheduler_G = torch.optim.lr_scheduler.StepLR(optimizer=optiG, step_size=step_size, gamma=gamma)
        lr_scheduler_D = torch.optim.lr_scheduler.StepLR(optimizer=optiD, step_size=step_size, gamma=gamma)

    # losses
    mse = nn.MSELoss()
    bce = nn.BCELoss()
    wass = lambda y_true, y_pred: torch.mean(y_true * y_pred)
    criterion = wass if wgan else bce

    # history
    losses = {'G': [], 'D': []} #, 'LHS': []}
    mses = {'train': [], 'val': []} if train_mse else {'val': []}
    preds = {'pred': [], 'soln': []}
    resids = {'grid': [], 'resid': []}

    start_time = time.time()
    for epoch in range(niters):
        
        # Train Generator
        for p in D.parameters():
            p.requires_grad = False # turn off computation for D

        for _ in range(G_iters):
            if method == 'unsupervised':
                grid_samp = problem.get_grid_sample(grid_samp, residuals, residuals_delta)
                #grid_samp_delta = grid_samp + 1e-5
                pred = G(grid_samp)
                #pred_delta = G(grid_samp_delta)
                residuals = problem.get_equation(pred, grid_samp)
                #residuals_delta = problem.get_equation(pred_delta, grid_samp_delta)
                
                # idea: add noise to relax from dirac delta at 0 to distb'n
                if noise:
                    diff = diff if diff >= 0 else 0
                    real = torch.zeros_like(residuals) + torch.normal(0, diff, size=residuals.shape)
                    fake = residuals + torch.normal(0, diff, size=residuals.shape)
                else:
                    real = torch.zeros_like(residuals)
                    fake = residuals
                # fake = residuals

                if conditional:
                    real = torch.cat((real, grid_samp), 1)
                    fake = torch.cat((fake, grid_samp), 1)
                
                optiG.zero_grad()
                g_loss = criterion(D(fake), real_labels)
                # g_loss = criterion(D(fake), torch.ones_like(fake))
                g_loss.backward()
                optiG.step()

            elif method == 'semisupervised':
                # unsupervised part (use GAN)
                grid_samp = problem.get_grid_sample()
                pred = G(grid_samp)
                residuals = problem.get_equation(pred, grid_samp)

                real = torch.zeros_like(residuals)
                fake = residuals

                if conditional:
                    real = torch.cat((real, grid_samp), 1)
                    fake = torch.cat((fake, grid_samp), 1)

                g_loss1 = criterion(D(fake), real_labels)

                # supervised part (use L2)
                pred = G(grid_obs)
                pred_adj = problem.adjust(pred, grid_obs)[0]
                g_loss2 = mse(pred_adj, soln_obs)

                # combine losses
                g_loss = d1 * g_loss1 + d2 * g_loss2
                optiG.zero_grad()
                g_loss.backward(retain_graph=True)
                optiG.step()

            else: # supervised
                # @note: Why removed for now?
                # the discriminator below uses real_labels/fake_labels
                # and NOT real_labels_obs/fake_labels_obs such that it is
                # compatible both with unsupervised and semi-supervised (in
                # the case where the GAN is used for the unsupervised portion)
                raise NotImplementedError()

                # xhat = G(t_obs)
                # xadj = problem.adjust(xhat, t_obs)[0]
                # residuals = y_obs - xadj
                #
                # if conditional:
                #     # concat "real" (y) with t (conditional GAN)
                #     # concat "fake" (xadj) with t (conditional GAN)
                #     real = torch.cat((torch.zeros_like(residuals), t_obs), 1)
                #     fake = torch.cat((residuals, t_obs), 1)
                # else:
                #     real = torch.zeros_like(residuals)
                #     fake = residuals
                #
                # g_loss = criterion(D(fake), real_labels_obs)
                # optiG.zero_grad()
                # g_loss.backward(retain_graph=True)
                # optiG.step()

        # Train Discriminator
        for p in D.parameters():
            p.requires_grad = True # turn on computation for D

        for _ in range(D_iters):
            if wgan:
                norm_penalty = calc_gradient_penalty(D, real, fake, gp, cuda=False)
            else:
                norm_penalty = torch.zeros(1)

            # print(real.shape, fake.shape)
            real_loss = criterion(D(real), real_labels)
            # real_loss = criterion(D(real), torch.ones_like(real))
            fake_loss = criterion(D(fake.detach()), fake_labels)
            # fake_loss = criterion(D(fake), torch.zeros_like(fake))
            
            optiD.zero_grad()
            d_loss = (real_loss + fake_loss)/2 + norm_penalty
            d_loss.backward()
            optiD.step()

        if noise:
            diff = g_loss.item() - d_loss.item()

        losses['D'].append(d_loss.item())
        # losses['D_real'].append(real_loss.item())
        # losses['D_fake'].append(fake_loss.item())
        losses['G'].append(g_loss.item())
        # losses['LHS'].append(torch.mean(torch.abs(fake)).item())
        
        if lr_schedule:
            lr_scheduler_G.step()
            lr_scheduler_D.step()

        # train MSE: grid sample vs true soln
        # grid_samp, sort_ids = torch.sort(grid_samp, axis=0)
        if train_mse:
            pred = G(grid_samp)
            pred_adj = problem.adjust(pred, grid_samp)['pred']
            sol_samp = problem.get_solution(grid_samp)
            train_mse = mse(pred_adj, sol_samp).item()
            mses['train'].append(train_mse)

        # val MSE: fixed grid vs true soln
        val_pred = G(grid)
        val_pred_adj = problem.adjust(val_pred, grid)['pred']
        val_mse = mse(val_pred_adj, soln).item()
        mses['val'].append(val_mse)

        # save preds and resids for animation
        if save_for_animation or multihead:
            preds['pred'].append(val_pred_adj.detach())
            preds['soln'].append(soln.detach())
            resids['grid'].append(grid_samp.detach())
            resids['resid'].append(np.abs(residuals.detach()))

        try:
            if (epoch+1) % 10 == 0:
                # mean of val mses for last 10 steps
                tune.report(mean_squared_error=np.mean(mses['val'][-10:]))
                # mean of G - D loss for last 10 steps
                # loss_diff = np.mean(np.abs(losses['G'][-10] - losses['D'][-10]))
                # report.log(mean_squared_error=loss_diff)
        except Exception as e:
            # print(f'Caught exception {e}')
            pass

        if log:
            if train_mse:
                print(f'Step {epoch}: G Loss: {g_loss.item():.4e} | D Loss: {d_loss.item():.4e} | Train MSE {train_mse:.4e} | Val MSE {val_mse:.4e}')
            else:
                print(f'Step {epoch}: G Loss: {g_loss.item():.4e} | D Loss: {d_loss.item():.4e} | Val MSE {val_mse:.4e}')

    # get pred and diff dicts
    end_time = time.time() - start_time
    print(f"Run completed in {end_time:.8f} seconds.")
    plot_soln = problem.get_plot_solution(grid)
    pred_dict, diff_dict = problem.get_plot_dicts(G(grid), grid, plot_soln)
    
    if plot:
        if multihead:
            plot_multihead(mses, losses, resids, save=save, dirname=dirname, alpha=0.7)
        else:
            plot_grid = problem.get_plot_grid()
            plot_results(mses, losses, plot_grid.detach(), pred_dict, diff_dict=diff_dict,
                save=save, dirname=dirname, logloss=False, alpha=0.7, plot_sep_curves=plot_sep_curves)

    if save:
        write_config(config, os.path.join(dirname, 'config.yaml'))
        np.save(os.path.join(dirname, "mses"), mses)
        if multihead:
            np.save(os.path.join(dirname, "resid_dict"), resids)

    if save_G:
        torch.save(G.state_dict(), f"config/pretrained_nets/{pkey}_gen.pth")

    if save_for_animation:
        if not os.path.exists(dirname):
            os.mkdir(dirname)
        anim_dir = os.path.join(dirname, "animation")
        print(f'Saving animation traces to {anim_dir}')
        if not os.path.exists(anim_dir):
            os.mkdir(anim_dir)
        np.save(os.path.join(anim_dir, "grid"), grid.detach())
        for k, v in preds.items():
            v = np.hstack(v)
            # TODO: for systems (i.e. multi-dim preds),
            # hstack flattens preds, need to use dstack
            # v = np.dstack(v)
            np.save(os.path.join(anim_dir, f"{k}_pred"), v)
        for k, v in resids.items():
            v = np.hstack(v)
            np.save(os.path.join(anim_dir, f"{k}_resid"), v)

    return {'mses': mses, 'model': G, 'losses': losses, 'preds': pred_dict}

def train_L2(model, problem, method='unsupervised', niters=100,
    lr=1e-3, betas=(0, 0.9), lr_schedule=True, gamma=0.999, step_size=15,
    obs_every=1, d1=1, d2=1, log=True, plot=True, save=False,
    dirname='train_L2', config=None, loss_fn=None, save_for_animation=False,
    **kwargs):
    """
    Train/test Lagaris method: supervised/semisupervised/unsupervised
    """
    assert method in ['supervised', 'semisupervised', 'unsupervised'], f'Method {method} not understood!'

    dirname = os.path.join(this_dir, '../experiments/runs', dirname)
    if plot and save:
        handle_overwrite(dirname)

    # validation: fixed grid/solution
    grid = problem.get_grid()
    sol = problem.get_solution(grid)

    # observers = torch.arange(0, len(grid), obs_every)
    # grid_obs = grid[observers, :]
    # sol_obs = sol[observers, :]

    # optimizers & loss functions
    opt = torch.optim.Adam(model.parameters(), lr=lr, betas=betas)
    if loss_fn:
        mse = eval(f"torch.nn.{loss_fn}()")
    else:
        mse = torch.nn.MSELoss()
    # lr scheduler
    if lr_schedule:
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer=opt, step_size=step_size, gamma=gamma)

    loss_trace = []
    mses = {'train': [], 'val': []}
    preds = {'pred': [], 'soln': []}

    for i in range(niters):
        if method == 'unsupervised':
            grid_samp = problem.get_grid_sample()
            pred = model(grid_samp)
            residuals = problem.get_equation(pred, grid_samp)
            loss = mse(residuals, torch.zeros_like(residuals))
            loss_trace.append(loss.item())

        elif method == 'semisupervised':
            # supervised part
            pred = model(grid_obs)
            pred_adj = problem.adjust(pred, grid_obs)[0]
            loss1 = mse(pred_adj, sol_obs)

            # unsupervised part
            grid_samp = problem.get_grid_sample()
            pred = model(grid_samp)
            residuals = problem.get_equation(pred, grid_samp)
            loss2 = mse(residuals, torch.zeros_like(residuals))

            # combine together
            loss = d1 * loss1 + d2 * loss2
            loss_trace.append((loss1.item(), loss2.item()))

        else: # supervised
            pred = model(grid_obs)
            pred_adj = problem.adjust(pred, grid_obs)[0]
            loss = mse(pred_adj, sol_obs)
            loss_trace.append(loss.item())

        # train MSE: grid sample vs true soln
        # grid_samp, sort_ids = torch.sort(grid_samp, axis=0)
        pred = model(grid_samp)
        try:
            pred_adj = problem.adjust(pred, grid_samp)['pred']
            sol_samp = problem.get_solution(grid_samp)
            train_mse = mse(pred_adj, sol_samp).item()
        except Exception as e:
            print(f'Exception: {e}')
        mses['train'].append(train_mse)

        # val MSE: fixed grid vs true soln
        val_pred = model(grid)
        val_pred_adj = problem.adjust(val_pred, grid)['pred']
        val_mse = mse(val_pred_adj, sol).item()
        mses['val'].append(val_mse)

        # store preds for animation
        preds['pred'].append(val_pred_adj.detach())
        preds['soln'].append(sol.detach())

        try:
            if (i+1) % 10 == 0:
                # mean of val mses for last 10 steps
                tune.report(mean_squared_error=np.mean(mses['val'][-10:]))
        except Exception as e:
            # print(f'Caught exception {e}')
            pass

        if log:
            print(f'Step {i}: Loss {loss.item():.4e} | Train MSE {train_mse:.4e} | Val MSE {val_mse:.4e}')

        opt.zero_grad()
        loss.backward(retain_graph=True)
        opt.step()
        if lr_schedule:
            lr_scheduler.step()

    if plot:
        loss_dict = {}
        if method == 'supervised':
            loss_dict['$L_S$'] = loss_trace
        elif method == 'semisupervised':
            loss_dict['$L_S$'] = [l[0] for l in loss_trace]
            loss_dict['$L_U$'] = [l[1] for l in loss_trace]
        else:
            loss_dict['$L_U$'] = loss_trace

        save_to = os.path.join(this_dir, '../experiments/runs', dirname)

        pred_dict, diff_dict = problem.get_plot_dicts(model(grid), grid, sol)
        plot_results(mses, loss_dict, grid.detach(), pred_dict, diff_dict=diff_dict,
            save=save, dirname=dirname, logloss=True, alpha=0.7)

    if save:
        write_config(config, os.path.join(dirname, 'config.yaml'))

    if save_for_animation:
        if not os.path.exists(dirname):
            os.mkdir(dirname)
        anim_dir = os.path.join(dirname, "animation")
        print(f'Saving animation traces to {anim_dir}')
        if not os.path.exists(anim_dir):
            os.mkdir(anim_dir)
        np.save(os.path.join(anim_dir, "grid"), grid.detach())
        for k, v in preds.items():
            v = np.hstack(v)
            # TODO: for systems (i.e. multi-dim preds),
            # hstack flattens preds, need to use dstack
            # v = np.dstack(v)
            np.save(os.path.join(anim_dir, f"{k}_pred"), v)

    return {'mses': mses, 'model': model, 'losses': loss_trace}

def train_GAN_2D(G, D, problem, method='unsupervised', niters=100,
    g_lr=1e-3, g_betas=(0.0, 0.9), d_lr=1e-3, d_betas=(0.0, 0.9),
    lr_schedule=True, gamma=0.999, momentum=0.95, noise=False, 
    step_size=15, obs_every=1, d1=1., d2=1., G_iters=1, D_iters=1, 
    wgan=True, gp=0.1, conditional=True, train_mse=True, log=True, 
    plot=True, plot_1d_curves=False, save=False, dirname='train_GAN', 
    config=None, save_for_animation=False, view=[35, -55], **kwargs):
    """
    Train/test GAN method: supervised/semisupervised/unsupervised
    """
    assert method in ['supervised', 'semisupervised', 'unsupervised'], f'Method {method} not understood!'

    pkey = dirname.split('_')[0].lower()
    dirname = os.path.join(this_dir, '../experiments/runs', dirname)
    if plot and save:
        handle_overwrite(dirname)

    # validation: fixed grid/solution
    x, y = problem.get_grid()
    grid = torch.cat((x, y), 1)
    soln = problem.get_solution(x, y)

    # initialize residuals and grid for active sampling
    grid_samp = grid
    residuals = torch.zeros_like(x)
    residuals_delta = torch.zeros_like(grid_samp)

    # grid for plotting and animation
    if plot or save_for_animation:
        dims = problem.get_plot_dims()
        plot_x, plot_y = problem.get_plot_grid()
        plot_grid = torch.cat((plot_x, plot_y), 1)
        plot_soln = problem.get_plot_solution(plot_x, plot_y)

    # observer mask and masked grid/solution (t_obs/y_obs)
    observers = torch.arange(0, len(grid), obs_every)
    # grid_obs = grid[observers, :]
    # soln_obs = soln[observers, :]

    # labels
    real_label = 1
    fake_label = -1 if wgan else 0
    real_labels = torch.full((len(grid),), real_label, dtype=torch.float).reshape(-1,1) #len(grid)+195 for no reparam
    fake_labels = torch.full((len(grid),), fake_label, dtype=torch.float).reshape(-1,1) #len(grid)+195 for no reparam
    # masked label vectors
    real_labels_obs = real_labels[observers, :]
    fake_labels_obs = fake_labels[observers, :]

    # initialize difference parameter for noise
    if noise:
        diff = 0

    # optimization
    optiG = torch.optim.Adam(G.parameters(), lr=g_lr, betas=g_betas)
    optiD = torch.optim.Adam(D.parameters(), lr=d_lr, betas=d_betas)
    #optiG = torch.optim.SGD(G.parameters(), lr=g_lr, momentum=momentum, nesterov=True)
    #optiD = torch.optim.SGD(D.parameters(), lr=g_lr, momentum=momentum, nesterov=True)
    if lr_schedule:
        #lr_scheduler_G = torch.optim.lr_scheduler.ExponentialLR(optimizer=optiG, gamma=gamma)
        #lr_scheduler_D = torch.optim.lr_scheduler.ExponentialLR(optimizer=optiD, gamma=gamma)
        lr_scheduler_G = torch.optim.lr_scheduler.StepLR(optimizer=optiG, step_size=step_size, gamma=gamma)
        lr_scheduler_D = torch.optim.lr_scheduler.StepLR(optimizer=optiD, step_size=step_size, gamma=gamma)

    # losses
    mse = nn.MSELoss()
    bce = nn.BCELoss()
    wass = lambda y_true, y_pred: torch.mean(y_true * y_pred)
    criterion = wass if wgan else bce

    # history
    losses = {'G': [], 'D': []} #, 'LHS': []}
    mses = {'train': [], 'val': []} if train_mse else {'val': []}
    preds = {'pred': [], 'soln': []}
    resids = {'grid': [], 'resid': []}

    for epoch in range(niters):
        # Train Generator
        for p in D.parameters():
            p.requires_grad = False # turn off computation for D

        for i in range(G_iters):
            xs, ys = problem.get_grid_sample(grid_samp, residuals, residuals_delta)
            grid_samp = torch.cat((xs, ys), 1)
            # grid_samp_deltax = torch.cat((xs+1e-5, ys), 1)
            # grid_samp_deltay = torch.cat((xs, ys+1e-5), 1)
            pred = G(grid_samp)
            # pred_deltax = G(grid_samp_deltax)
            # pred_deltay = G(grid_samp_deltay)
            residuals = problem.get_equation(pred, xs, ys)
            # residuals_deltax = problem.get_equation(pred_deltax, xs+1e-5, ys)
            # residuals_deltay = problem.get_equation(pred_deltay, xs, ys+1e-5)
            # residuals_delta = torch.cat((residuals_deltax, residuals_deltay), 1)
            # if epoch == 1000:
            #     problem.sampling = 'gradient'
            #     fig, ax = plt.subplots(figsize=(12,12))
            #     xx, yy = xs.detach().numpy().reshape((65,65)), ys.detach().numpy().reshape((65,65))
            #     resid = residuals.detach().numpy().reshape((65,65))
            #     ax.contourf(xx, yy, resid, cmap='Reds')
            #     plt.show()
            #     assert False
            # idea: add noise to relax from dirac delta at 0 to distb'n
            # + torch.normal(0, .1/(i+1), size=residuals.shape)
            if noise:
                diff = diff if diff >= 0 else 0
                real = torch.zeros_like(residuals) + torch.normal(0, diff, size=residuals.shape)
                fake = residuals + torch.normal(0, diff, size=residuals.shape)
            else:
                real = torch.zeros_like(residuals)
                fake = residuals
            # fake = residuals

            optiG.zero_grad()
            g_loss = criterion(D(fake), real_labels)
            # g_loss = criterion(D(fake), torch.ones_like(fake))
            g_loss.backward()
            optiG.step()

        # Train Discriminator
        for p in D.parameters():
            p.requires_grad = True # turn on computation for D

        for i in range(D_iters):
            if wgan:
                norm_penalty = calc_gradient_penalty(D, real, fake, gp, cuda=False)
            else:
                norm_penalty = torch.zeros(1)

            # print(real.shape, fake.shape)
            real_loss = criterion(D(real), real_labels)
            # real_loss = criterion(D(real), torch.ones_like(real))
            fake_loss = criterion(D(fake.detach()), fake_labels)
            # fake_loss = criterion(D(fake), torch.zeros_like(fake))

            optiD.zero_grad()
            d_loss = (real_loss + fake_loss)/2 + norm_penalty
            d_loss.backward()
            optiD.step()

        # update the difference between losses
        if noise:
            diff = g_loss.item() - d_loss.item()

        losses['D'].append(d_loss.item())
        losses['G'].append(g_loss.item())
        # losses['LHS'].append(torch.mean(torch.abs(fake)).item())

        if lr_schedule:
          lr_scheduler_G.step()
          lr_scheduler_D.step()

        # train MSE: grid sample vs true soln
        # grid_samp, sort_ids = torch.sort(grid_samp, axis=0)
        if train_mse:
            pred = G(grid_samp)
            pred_adj = problem.adjust(pred, xs, ys)['pred']
            sol_samp = problem.get_solution(xs, ys)
            train_mse = mse(pred_adj, sol_samp).item()
            mses['train'].append(train_mse)

        # val MSE: fixed grid vs true soln
        val_pred = G(grid)
        val_pred_adj = problem.adjust(val_pred, x, y)['pred']
        val_mse = mse(val_pred_adj, soln).item()
        mses['val'].append(val_mse)

        # save preds and residuals for animation
        if save_for_animation:
            plot_pred = G(plot_grid)
            plot_pred_adj = problem.adjust(plot_pred, plot_x, plot_y)['pred']
            preds['pred'].append(plot_pred_adj.detach())
            preds['soln'].append(plot_soln.detach())
            resids['grid'].append(grid_samp.detach())
            resids['resid'].append(np.abs(residuals.detach()))

        try:
            if (epoch+1) % 10 == 0:
                # mean of val mses for last 10 steps
                #track.log(lhs=np.mean(losses['LHS'][-10:])) # mean LHS for last 10 steps
                tune.report(mean_squared_error=np.mean(mses['val'][-10:]))

        except Exception as e:
            # print(f'Caught exception {e}')
            pass

        if log:
            if train_mse:
                print(f'Step {epoch}: G Loss: {g_loss.item():.4e} | D Loss: {d_loss.item():.4e} | Train MSE {train_mse:.4e} | Val MSE {val_mse:.4e}')
            else:
                print(f'Step {epoch}: G Loss: {g_loss.item():.4e} | D Loss: {d_loss.item():.4e} | Val MSE {val_mse:.4e}')
    
    if plot:
        pred_dict, diff_dict = problem.get_plot_dicts(G(plot_grid), plot_x, plot_y, plot_soln)
        plot_results(mses, losses, plot_grid.detach(), pred_dict, diff_dict=diff_dict,
            save=save, dirname=dirname, logloss=False, alpha=0.7, dims=dims,
            plot_1d_curves=plot_1d_curves)
        plot_3D(plot_grid.detach(), pred_dict, view=view, dims=dims, save=save, dirname=dirname)

    if save:
        write_config(config, os.path.join(dirname, 'config.yaml'))
        np.save(os.path.join(dirname, "pred_dict"), pred_dict)

    if save_for_animation:
        if not os.path.exists(dirname):
            os.mkdir(dirname)
        anim_dir = os.path.join(dirname, "animation")
        print(f'Saving animation traces to {anim_dir}')
        if not os.path.exists(anim_dir):
            os.mkdir(anim_dir)
        np.save(os.path.join(anim_dir, "grid"), plot_grid.detach())
        for k, v in preds.items():
            v = np.hstack(v)
            # TODO: for systems (i.e. multi-dim preds),
            # hstack flattens preds, need to use dstack
            # v = np.dstack(v)
            np.save(os.path.join(anim_dir, f"{k}_pred"), v)
        for k, v in resids.items():
            v = np.hstack(v)
            np.save(os.path.join(anim_dir, f"{k}_resid"), v)

    return {'mses': mses, 'model': G, 'losses': losses}

def train_L2_2D(model, problem, method='unsupervised', niters=100,
    lr=1e-3, betas=(0, 0.9), lr_schedule=True, gamma=0.999, step_size=15,
    obs_every=1, d1=1, d2=1, log=True, plot=True, save=False,
    dirname='train_L2', config=None, loss_fn=None, save_for_animation=False,
    **kwargs):
    """
    Train/test Lagaris method: supervised/semisupervised/unsupervised
    """
    assert method in ['supervised', 'semisupervised', 'unsupervised'], f'Method {method} not understood!'

    dirname = os.path.join(this_dir, '../experiments/runs', dirname)
    if plot and save:
        handle_overwrite(dirname)

    # validation: fixed grid/solution
    x, y = problem.get_grid()
    grid = torch.cat((x, y), 1)
    sol = problem.get_solution(x, y)

    # optimizers & loss functions
    opt = torch.optim.Adam(model.parameters(), lr=lr, betas=betas)
    if loss_fn:
        mse = eval(f"torch.nn.{loss_fn}()")
    else:
        mse = torch.nn.MSELoss()
    # lr scheduler
    if lr_schedule:
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer=opt, step_size=step_size, gamma=gamma)

    loss_trace = []
    mses = {'train': [], 'val': []}
    preds = {'pred': [], 'soln': []}

    for i in range(niters):
        xs, ys = problem.get_grid_sample(None, None, None) #TODO: remove these unnecessary args
        grid_samp = torch.cat((xs, ys), 1)
        pred = model(grid_samp)
        residuals = problem.get_equation(pred, xs, ys)
        loss = mse(residuals, torch.zeros_like(residuals))
        loss_trace.append(loss.item())

        # train MSE: grid sample vs true soln
        # grid_samp, sort_ids = torch.sort(grid_samp, axis=0)
        pred = model(grid_samp)
        try:
            pred_adj = problem.adjust(pred, xs, ys)['pred']
            sol_samp = problem.get_solution(xs, ys)
            train_mse = mse(pred_adj, sol_samp).item()
        except Exception as e:
            print(f'Exception: {e}')
        mses['train'].append(train_mse)

        # val MSE: fixed grid vs true soln
        val_pred = model(grid)
        val_pred_adj = problem.adjust(val_pred, x, y)['pred']
        val_mse = mse(val_pred_adj, sol).item()
        mses['val'].append(val_mse)

        # store preds for animation
        preds['pred'].append(val_pred_adj.detach())
        preds['soln'].append(sol.detach())

        try:
            if (i+1) % 10 == 0:
                # mean of val mses for last 10 steps
                tune.report(mean_squared_error=np.mean(mses['val'][-10:]))
        except Exception as e:
            # print(f'Caught exception {e}')
            pass

        if log:
            print(f'Step {i}: Loss {loss.item():.4e} | Train MSE {train_mse:.4e} | Val MSE {val_mse:.4e}')

        opt.zero_grad()
        loss.backward(retain_graph=True)
        opt.step()
        if lr_schedule:
            lr_scheduler.step()

    if plot:
        loss_dict = {}
        if method == 'supervised':
            loss_dict['$L_S$'] = loss_trace
        elif method == 'semisupervised':
            loss_dict['$L_S$'] = [l[0] for l in loss_trace]
            loss_dict['$L_U$'] = [l[1] for l in loss_trace]
        else:
            loss_dict['$L_U$'] = loss_trace
        dims = problem.get_plot_dims()

        save_to = os.path.join(this_dir, '../experiments/runs', dirname)

        pred_dict, diff_dict = problem.get_plot_dicts(model(grid), x, y, sol)
        plot_results(mses, loss_dict, grid.detach(), pred_dict, diff_dict=diff_dict,
            save=save, dirname=dirname, logloss=True, alpha=0.7, dims=dims)

    if save:
        write_config(config, os.path.join(dirname, 'config.yaml'))

    if save_for_animation:
        if not os.path.exists(dirname):
            os.mkdir(dirname)
        anim_dir = os.path.join(dirname, "animation")
        print(f'Saving animation traces to {anim_dir}')
        if not os.path.exists(anim_dir):
            os.mkdir(anim_dir)
        np.save(os.path.join(anim_dir, "grid"), grid.detach())
        for k, v in preds.items():
            v = np.hstack(v)
            # TODO: for systems (i.e. multi-dim preds),
            # hstack flattens preds, need to use dstack
            # v = np.dstack(v)
            np.save(os.path.join(anim_dir, f"{k}_pred"), v)

    return {'mses': mses, 'model': model, 'losses': loss_trace}
